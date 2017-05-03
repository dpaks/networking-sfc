# Copyright 2015 Huawei.
# Copyright 2016 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy

from neutron_lib import constants as n_consts
from oslo_config import cfg
from oslo_log import log as logging

from neutron.plugins.ml2.drivers.openvswitch.agent.common import constants \
    as ovs_consts
from neutron.plugins.ml2.drivers.openvswitch.agent import vlanmanager

from networking_sfc._i18n import _LE
from networking_sfc.services.sfc.agent.extensions import sfc
from networking_sfc.services.sfc.common import ovs_ext_lib
from networking_sfc.services.sfc.drivers.ovs import constants

LOG = logging.getLogger(__name__)

cfg.CONF.import_group('OVS', 'neutron.plugins.ml2.drivers.openvswitch.agent.'
                             'common.config')

# This table is used to process the traffic across differet subnet scenario.
# Flow 1: pri=1, ip,dl_dst=nexthop_mac,nw_src=nexthop_subnet. actions=
# push_mpls:0x8847,set_mpls_label,set_mpls_ttl,push_vlan,output:(patch port
# or resubmit to table(INGRESS_TABLE)
# Flow 2: pri=0, ip,dl_dst=nexthop_mac,, action=push_mpls:0x8847,
# set_mpls_label,set_mpls_ttl,push_vlan,output:(patch port or resubmit to
# table(INGRESS_TABLE)
ACROSS_SUBNET_TABLE = 5

# The table has multiple flows that steer traffic for the different chains
# to the ingress port of different service functions hosted on this Compute
# node.
INGRESS_TABLE = 10

# port chain default flow rule priority
PC_DEF_PRI = 20
PC_INGRESS_PRI = 30

sfc_ovs_opt = [
        cfg.StrOpt('local_hostname',
                   default='', help='Hostname of the local machine')]
cfg.CONF.register_opts(sfc_ovs_opt, 'OVS')


class SfcOVSAgentDriver(sfc.SfcAgentDriver):
    """This class will support MPLS frame

    Ethernet + MPLS
    IPv4 Packet:
    +-------------------------------+---------------+--------------------+
    |Outer Ethernet, ET=0x8847      | MPLS head,    | original IP Packet |
    +-------------------------------+---------------+--------------------+
    """

    REQUIRED_PROTOCOLS = [
        ovs_consts.OPENFLOW10,
        ovs_consts.OPENFLOW11,
        ovs_consts.OPENFLOW12,
        ovs_consts.OPENFLOW13,
    ]

    def __init__(self):
        super(SfcOVSAgentDriver, self).__init__()
        self.agent_api = None
        self.br_int = None

        self.local_ip = None
        self.local_host = None
        self.patch_tun_ofport = None
        self.vlan_manager = None

    def consume_api(self, agent_api):
        self.agent_api = agent_api

    def initialize(self):
        self.br_int = ovs_ext_lib.SfcOVSBridgeExt(
            self.agent_api.request_int_br())
        self.br_int.set_protocols(SfcOVSAgentDriver.REQUIRED_PROTOCOLS)

        self.local_ip = cfg.CONF.OVS.local_ip
        self.local_host = cfg.CONF.OVS.local_hostname
        '''self.patch_tun_ofport = self.br_int.get_port_ofport(
            cfg.CONF.OVS.int_peer_patch_port)'''
        self.patch_tun_ofport = self.br_int.get_port_ofport("int-br-vlan")
        self.vlan_manager = vlanmanager.LocalVlanManager()

        self._clear_sfc_flow_on_int_br()

    def update_flow_rules(self, flowrule, flowrule_status):
        try:
            if flowrule.get('egress', None):
                self._setup_egress_flow_rules(flowrule)
                self._setup_reverse_ingress_flow_rules(flowrule)
            if flowrule.get('ingress', None):
                self._setup_ingress_flow_rules_with_mpls(flowrule)
                self._setup_reverse_egress_flow_rules(flowrule)

            flowrule_status_temp = {}
            flowrule_status_temp['id'] = flowrule['id']
            flowrule_status_temp['status'] = constants.STATUS_ACTIVE
            flowrule_status.append(flowrule_status_temp)
        except Exception as e:
            flowrule_status_temp = {}
            flowrule_status_temp['id'] = flowrule['id']
            flowrule_status_temp['status'] = constants.STATUS_ERROR
            flowrule_status.append(flowrule_status_temp)
            LOG.exception(e)
            LOG.error(_LE("update_flow_rules failed"))

    def delete_flow_rule(self, flowrule, flowrule_status):
        try:
            LOG.debug("delete_flow_rule, flowrule = %s",
                      flowrule)

            node_type = flowrule['node_type']
            # delete tunnel table flow rule on br-int(egress match)
            if flowrule['egress'] is not None:
                self._setup_local_switch_flows_on_int_br(
                    flowrule,
                    flowrule['del_fcs'],
                    None,
                    add_flow=False,
                    match_inport=True
                )
                self._setup_source_based_flows(
                    flowrule, flowrule['del_fcs'], add_flow=False)
                # delete group table, need to check again
                group_id = flowrule.get('next_group_id', None)
                if group_id and flowrule.get('group_refcnt', None) <= 1:
                    self.br_int.delete_group(group_id=group_id)
                    for item in flowrule['next_hops']:
                        self.br_int.delete_flows(
                            table=ACROSS_SUBNET_TABLE,
                            dl_dst=item['mac_address'])

            if flowrule['ingress'] is not None:
                self._setup_destination_based_forwarding(flowrule,
                                                         flowrule['del_fcs'],
                                                         add_flow=False)
                # delete table INGRESS_TABLE ingress match flow rule
                # on br-int(ingress match)
                vif_port = self.br_int.get_vif_port_by_id(flowrule['ingress'])
                if vif_port:
                    # third, install br-int flow rule on table INGRESS_TABLE
                    # for ingress traffic
                    self.br_int.delete_flows(
                        table=INGRESS_TABLE,
                        dl_type=0x8847,
                        dl_dst=vif_port.vif_mac,
                        mpls_label=flowrule['nsp'] << 8 | (flowrule['nsi'] + 1)
                    )
            if flowrule.get('reverse_path'):
                rev_flowrule = self._reverse_flow_rules(flowrule, node_type)
                if flowrule['ingress'] is not None:
                    self._setup_source_based_flows(
                        rev_flowrule, rev_flowrule['del_fcs'], add_flow=False)
                else:
                    self._setup_destination_based_forwarding(
                            rev_flowrule, rev_flowrule['del_fcs'],
                            add_flow=False)
        except Exception as e:
            flowrule_status_temp = {}
            flowrule_status_temp['id'] = flowrule['id']
            flowrule_status_temp['status'] = constants.STATUS_ERROR
            flowrule_status.append(flowrule_status_temp)
            LOG.exception(e)
            LOG.error(_LE("delete_flow_rule failed"))

    def _reverse_flow_rules(self, flowrule, node_type):
        rev_flowrule = copy.deepcopy(flowrule)

        def _reverse_fcs(op):
            for fc in rev_flowrule[op]:
                fc['logical_destination_port'], fc['logical_source_port'] = (
                    fc['logical_source_port'], fc['logical_destination_port'])
                fc['ldp_mac_address'], fc['lsp_mac_address'] = (
                    fc['lsp_mac_address'], fc['ldp_mac_address'])
                fc['destination_ip_prefix'], fc['source_ip_prefix'] = (
                    fc['source_ip_prefix'], fc['destination_ip_prefix'])

        for op in ['add_fcs', 'del_fcs']:
            _reverse_fcs(op)

        if node_type == 'src_node':
            rev_flowrule['ingress'], rev_flowrule['egress'] = (
                        rev_flowrule['egress'], rev_flowrule['ingress'])

        return rev_flowrule

    def _setup_reverse_ingress_flow_rules(self, flowrule):
        if not flowrule['reverse_path']:
            return
        rev_flowrule = self._reverse_flow_rules(flowrule,
                                                flowrule['node_type'])
        self._setup_ingress_flow_rules_with_mpls(rev_flowrule)

    def _setup_reverse_egress_flow_rules(self, flowrule):
        if not flowrule['reverse_path']:
            return
        rev_flowrule = self._reverse_flow_rules(flowrule,
                                                flowrule['node_type'])
        self._setup_egress_flow_rules(rev_flowrule)

    def _clear_sfc_flow_on_int_br(self):
        self.br_int.delete_group(group_id='all')
        self.br_int.delete_flows(table=ACROSS_SUBNET_TABLE)
        self.br_int.delete_flows(table=INGRESS_TABLE)
        self.br_int.install_goto(dest_table_id=INGRESS_TABLE,
                                 priority=PC_DEF_PRI,
                                 eth_type=0x8847)
        self.br_int.install_drop(table_id=INGRESS_TABLE)

    def _parse_flow_classifier(self, flow_classifier):
        dl_type, nw_proto, source_port_masks, destination_port_masks = (
            (None, ) * 4)

        if (
            not flow_classifier['source_port_range_min'] and
            not flow_classifier['source_port_range_max']
        ):
            # wildcard
            source_port_masks = ['0/0x0']
        elif not flow_classifier['source_port_range_min']:
            source_port_masks = ovs_ext_lib.get_port_mask(
                1,
                flow_classifier['source_port_range_max'])
        elif not flow_classifier['source_port_range_max']:
            source_port_masks = ovs_ext_lib.get_port_mask(
                flow_classifier['source_port_range_min'],
                65535)
        else:
            source_port_masks = ovs_ext_lib.get_port_mask(
                flow_classifier['source_port_range_min'],
                flow_classifier['source_port_range_max'])

        if (
            not flow_classifier['destination_port_range_min'] and
            not flow_classifier['destination_port_range_max']
        ):
            # wildcard
            destination_port_masks = ['0/0x0']
        elif not flow_classifier['destination_port_range_min']:
            destination_port_masks = ovs_ext_lib.get_port_mask(
                1,
                flow_classifier['destination_port_range_max'])
        elif not flow_classifier['destination_port_range_max']:
            destination_port_masks = ovs_ext_lib.get_port_mask(
                flow_classifier['destination_port_range_min'],
                65535)
        else:
            destination_port_masks = ovs_ext_lib.get_port_mask(
                flow_classifier['destination_port_range_min'],
                flow_classifier['destination_port_range_max'])

        if "IPv4" == flow_classifier['ethertype']:
            dl_type = 0x0800
            if n_consts.PROTO_NAME_TCP == flow_classifier['protocol']:
                nw_proto = n_consts.PROTO_NUM_TCP
            elif n_consts.PROTO_NAME_UDP == flow_classifier['protocol']:
                nw_proto = n_consts.PROTO_NUM_UDP
            elif n_consts.PROTO_NAME_ICMP == flow_classifier['protocol']:
                nw_proto = n_consts.PROTO_NUM_ICMP
            else:
                nw_proto = None
        elif "IPv6" == flow_classifier['ethertype']:
            LOG.error(_LE("Current portchain agent don't support Ipv6"))
        else:
            LOG.error(_LE("invalid protocol input"))
        return (dl_type, nw_proto,
                source_port_masks, destination_port_masks
                )

    def _get_flow_infos_from_flow_classifier(self, flow_classifier):
        flow_infos = []
        nw_src, nw_dst = ((None, ) * 2)

        if "IPv4" != flow_classifier['ethertype']:
            LOG.error(_LE("Current portchain agent don't support Ipv6"))
            return flow_infos

        # parse and transfer flow info to match field info
        dl_type, nw_proto, source_port_masks, destination_port_masks = (
            self._parse_flow_classifier(flow_classifier))

        if flow_classifier['source_ip_prefix']:
            nw_src = flow_classifier['source_ip_prefix']
        else:
            nw_src = '0.0.0.0/0.0.0.0'
        if flow_classifier['destination_ip_prefix']:
            nw_dst = flow_classifier['destination_ip_prefix']
        else:
            nw_dst = '0.0.0.0/0.0.0.0'

        if source_port_masks and destination_port_masks:
            for destination_port in destination_port_masks:
                for source_port in source_port_masks:
                    if nw_proto is None:
                        flow_infos.append(dict(
                            dl_type=dl_type,
                            nw_src=nw_src,
                            nw_dst=nw_dst,
                            tp_src='%s' % source_port,
                            tp_dst='%s' % destination_port
                        ))
                    else:
                        flow_infos.append(dict(
                            dl_type=dl_type,
                            nw_proto=nw_proto,
                            nw_src=nw_src,
                            nw_dst=nw_dst,
                            tp_src='%s' % source_port,
                            tp_dst='%s' % destination_port
                        ))

        return flow_infos

    def _get_flow_infos_from_flow_classifier_list(self, flow_classifier_list):
        flow_infos = []
        if not flow_classifier_list:
            return flow_infos
        for flow_classifier in flow_classifier_list:
            flow_infos.extend(
                self._get_flow_infos_from_flow_classifier(flow_classifier)
            )

        return flow_infos

    def _setup_local_switch_flows_on_int_br(
        self, flowrule, flow_classifier_list,
        actions, add_flow=True, match_inport=True
    ):
        inport_match = {}
        priority = PC_DEF_PRI

        if match_inport is True:
            egress_port = self.br_int.get_vif_port_by_id(flowrule['egress'])
            if egress_port:
                inport_match = dict(in_port=egress_port.ofport)
                priority = PC_INGRESS_PRI

        for flow_info in self._get_flow_infos_from_flow_classifier_list(
            flow_classifier_list
        ):
            match_info = dict(inport_match, **flow_info)
            if add_flow:
                self.br_int.add_flow(
                    table=ovs_consts.LOCAL_SWITCHING,
                    priority=priority,
                    actions=actions,
                    **match_info
                )
            else:
                self.br_int.delete_flows(
                    table=ovs_consts.LOCAL_SWITCHING,
                    priority=priority,
                    **match_info
                )

    def _setup_egress_flow_rules(self, flowrule, match_inport=True):
        group_id = flowrule.get('next_group_id', None)
        next_hops = flowrule.get('next_hops', None)
        global_vlan_tag = flowrule['segment_id']

        # if the group is not none, install the egress rule for this SF
        if (
            group_id and next_hops
        ):
            # 1st, install br-int flow rule on table ACROSS_SUBNET_TABLE
            # and group table
            buckets = []
            local_vlan = self._get_vlan_by_port(flowrule['egress'])
            # A2 Group Creation
            for item in next_hops:
                bucket = (
                    'bucket=weight=%d, mod_dl_dst:%s,'
                    'resubmit(,%d)' % (
                        item['weight'],
                        item['mac_address'],
                        ACROSS_SUBNET_TABLE
                    )
                )
                buckets.append(bucket)
                # A3 In table 5, add MPLS header and send to either patch port
                # or table 10 for remote and local node respectively.
                subnet_actions_list = []

                priority = 30
                if item['local_endpoint'] == self.local_host:
                    subnet_actions = (
                        "mod_vlan_vid:%d, resubmit(,%d)" % (global_vlan_tag,
                                                            INGRESS_TABLE))
                else:
                    # same subnet with next hop
                    subnet_actions = "output:%s" % self.patch_tun_ofport
                subnet_actions_list.append(subnet_actions)

                self.br_int.add_flow(
                    table=ACROSS_SUBNET_TABLE,
                    priority=priority,
                    dl_dst=item['mac_address'],
                    dl_type=0x0800,
                    actions="%s" % ','.join(subnet_actions_list))

            buckets = ','.join(buckets)
            group_content = self.br_int.dump_group_for_id(group_id)
            if group_content.find('group_id=%d' % group_id) == -1:
                self.br_int.add_group(group_id=group_id,
                                      type='select', buckets=buckets)
            else:
                self.br_int.mod_group(group_id=group_id,
                                      type='select', buckets=buckets)
        else:
            pass

        self._setup_source_based_flows(
            flowrule,
            flowrule['add_fcs'],
            add_flow=True,
            match_inport=True)

    def _update_flows(self, table, priority,
                      match_info, actions=None, add_flow=True):
        if add_flow:
            self.br_int.add_flow(table=table,
                                 priority=priority,
                                 actions=actions,
                                 **match_info)
        else:
            self.br_int.delete_flows(table=table,
                                     priority=priority,
                                     **match_info)

    def _check_if_local_port(self, port_id):
        try:
            if self.br_int.get_vif_port_by_id(port_id):
                return True
        except Exception:
            pass
        return False

    def _setup_source_based_flows(
        self, flowrule, flow_classifier_list,
        add_flow=True, match_inport=True
    ):
        inport_match = {}
        priority = 50
        global_vlan_tag = flowrule['segment_id']
        local_vlan_tag = self._get_vlan_by_port(flowrule['egress'])

        if match_inport is True:
            egress_port = self.br_int.get_vif_port_by_id(flowrule['egress'])
            if egress_port:
                inport_match = dict(in_port=egress_port.ofport)

        group_id = flowrule.get('next_group_id')
        next_hops = flowrule.get('next_hops')
        if not (group_id and next_hops):
            local_vlan_tag = self._get_vlan_by_port(flowrule['ingress'])
            egress_mac = egress_port.vif_mac
            # B6. For packets coming out of SF, we resubmit to table 5.
            match_info = dict(dl_type=0x0800, **inport_match)
            actions = ("resubmit(,%s)" % ACROSS_SUBNET_TABLE)

            self._update_flows(ovs_consts.LOCAL_SWITCHING, 60,
                               match_info, actions, add_flow)

            ingress_port = self.br_int.get_vif_port_by_id(flowrule['ingress'])
            ingress_mac = ingress_port.vif_mac
            # B7 In table 5, we decide whether to send it locally or remotely.
            for fc in flow_classifier_list:
                ldp_port_id = fc['logical_destination_port']
                ldp_mac = fc['ldp_mac_address']

                if self._check_if_local_port(ldp_port_id):
                    ldp_port = self.br_int.get_vif_port_by_id(ldp_port_id)
                    ldp_ofport = ldp_port.ofport
                    actions = ("strip_vlan, mod_dl_dst:%s, output:%s" % (
                                        ldp_mac, ldp_ofport))
                else:
                    actions = ("mod_vlan_vid:%s, mod_dl_src:%s, "
                               "mod_dl_dst:%s, output:%s" % (
                                   (local_vlan_tag, egress_mac,
                                    ldp_mac, self.patch_tun_ofport)))

                match_info = dict(nw_dst=fc['destination_ip_prefix'],
                                  dl_dst=ingress_mac,
                                  dl_type=0x0800,
                                  dl_vlan=global_vlan_tag)

                self._update_flows(ACROSS_SUBNET_TABLE, priority,
                                   match_info, actions, add_flow)
            return

        # A1. Flow inserted at LSP egress. Matches on ip, in_port and LDP IP.
        # Action is redirect to group.
        ldp_mac = flow_classifier_list[0]['ldp_mac_address']
        actions = ("mod_vlan_vid:%s, group:%d" % (
                                local_vlan_tag, group_id))
        match_info = dict(inport_match, **{'dl_dst': ldp_mac,
                                           'dl_type': '0x0800'})
        self._update_flows(ovs_consts.LOCAL_SWITCHING, priority,
                           match_info, actions, add_flow)

    def _get_port_info(self, port_id, info_type):
        ''' Returns specific port info

        @param port_id: Neutron port id
        @param info_type: Type is List [mac,ofport,vlan]
        @return: Tuple (MAC address, openflow port number)

        '''

        res = ()
        port = self.br_int.get_vif_port_by_id(port_id)

        port_type_map = {
            'mac': port.vif_mac,
            'ofport': port.ofport,
            'vlan': self._get_vlan_by_port(port_id)}

        for each in info_type:
            res += (port_type_map[each],)

        return res

    def _get_vlan_by_port(self, port_id):
        try:
            net_uuid = self.vlan_manager.get_net_uuid(port_id)
            return self.vlan_manager.get(net_uuid).vlan
        except (vlanmanager.VifIdNotFound, vlanmanager.MappingNotFound):
            return None

    def _setup_destination_based_forwarding(self, flowrule,
                                            flow_classifier_list,
                                            add_flow=True):
        priority = 50
        ingress_mac, ingress_ofport = self._get_port_info(
                                flowrule['ingress'], ['mac', 'ofport'])
        local_vlan_tag = self._get_vlan_by_port(flowrule['ingress'])
        global_vlan_tag = flowrule['segment_id']

        group_id = flowrule.get('next_group_id')
        next_hops = flowrule.get('next_hops')
        if not (group_id and next_hops):
            # B5. At ingress of SF, modify dest mac to that of Dest VM's
            # if nw_dst belongs to Dest VM and output to ingress port of SF.
            for fc in flow_classifier_list:
                ldp_mac = fc['ldp_mac_address']
                dst_ip = fc['destination_ip_prefix']

                match_info = dict(dl_type=0x0800,
                                  dl_vlan=global_vlan_tag,
                                  dl_dst=ingress_mac,
                                  nw_dst=dst_ip)
                actions = ("strip_vlan, output:%s" % (ingress_ofport))

                self._update_flows(INGRESS_TABLE, 60,
                                   match_info, actions, add_flow)

                # B4. At ingress of SF, if dest mac matches with SF ingress,
                # vlan matches, then resubmit to 10.
                # This is per ldp because ldps can have different vlan tags.
                match_info = dict(
                    dl_type=0x0800,
                    dl_vlan=global_vlan_tag, dl_dst=ingress_mac)
                actions = ("resubmit(,%s)" % INGRESS_TABLE)
                self._update_flows(ovs_consts.LOCAL_SWITCHING, priority,
                                   match_info, actions, add_flow)
            return

        # B9. Match IP packets with vlan and source IP. Actions will be to
        # strip vlan and modify src MAC and output to Dest VM port.
        nw_src = flow_classifier_list[0]['source_ip_prefix']

        src_port_mac = flow_classifier_list[0]['lsp_mac_address']
        actions = ('strip_vlan, mod_dl_src:%s, output:%s' % (
                                            src_port_mac, ingress_ofport))

        match_field = dict(
            dl_type=0x0800,
            dl_vlan=global_vlan_tag,
            dl_dst=ingress_mac,
            nw_src=nw_src)

        self._update_flows(10, priority,
                           match_field, actions, add_flow)

        # B8. At ingress of dest, match ip packet with vlan tag and dest
        # MAC address. Action will be to resubmit to 10.
        match_field = dict(
            dl_type=0x0800,
            dl_vlan=global_vlan_tag,
            dl_dst=ingress_mac)
        actions = ("resubmit(,%s)" % 10)

        self._update_flows(ovs_consts.LOCAL_SWITCHING, priority,
                           match_field, actions, add_flow)

    def _setup_ingress_flow_rules_with_mpls(self, flowrule):
        flow_classifier_list = flowrule['add_fcs']
        self._setup_destination_based_forwarding(flowrule,
                                                 flow_classifier_list)
