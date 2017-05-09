# Copyright e015 nuturewei. All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import netaddr

from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_serialization import jsonutils

from neutron import manager

from neutron.plugins.common import constants as np_const

from networking_sfc._i18n import _LE, _LW
from networking_sfc.services.sfc.common import exceptions as exc
from networking_sfc.services.sfc.drivers.ovs import (
    constants as ovs_const)
from networking_sfc.services.sfc.drivers.ovs import driver as sfc_dvr


LOG = logging.getLogger(__name__)


class OCSfcDriver(sfc_dvr.OVSSfcDriver):
    """Sfc Driver Base Class."""

    def initialize(self):
        super(OCSfcDriver, self).initialize()

    @log_helpers.log_method_call
    def _add_flowclassifier_port_assoc(self, fc_ids, tenant_id,
                                       src_node):
        for fc in self._get_fcs_by_ids(fc_ids):
            need_assoc = True
            src_pd_filter = dst_pd_filter = None

            if fc['logical_source_port']:
                # lookup the source port
                src_pd_filter = dict(
                    egress=fc['logical_source_port'],
                    tenant_id=tenant_id
                )
            if fc['logical_destination_port']:
                # lookup the source port
                dst_pd_filter = dict(
                    ingress=fc['logical_destination_port'],
                    tenant_id=tenant_id
                )
            src_pd = self.get_port_detail_by_filter(src_pd_filter)
            dst_pd = self.get_port_detail_by_filter(dst_pd_filter)

            new_src_pd = new_dst_pd = ''
            if not (src_pd and dst_pd):
                if not src_pd:
                    # Create source port detail
                    new_src_pd = self._create_port_detail(src_pd_filter)
                    LOG.debug('create src port detail: %s', new_src_pd)
                if not dst_pd:
                    # Create destination port detail
                    new_dst_pd = self._create_port_detail(dst_pd_filter)
                    LOG.debug('create dst port detail: %s', new_dst_pd)
            else:
                for path_node in src_pd['path_nodes']:
                    if path_node['pathnode_id'] == src_node['id']:
                        need_assoc = False
            if need_assoc:
                # Create associate relationship
                if new_src_pd:
                    assco_args = {
                        'portpair_id': new_src_pd['id'],
                        'pathnode_id': src_node['id'],
                        'weight': 1,
                    }
                    sna = self.create_pathport_assoc(assco_args)
                    LOG.debug('create assoc src port with node: %s', sna)
                    src_node['portpair_details'].append(new_src_pd['id'])
                if new_dst_pd:
                    assco_args = {
                        'portpair_id': new_dst_pd['id'],
                        'pathnode_id': src_node['id'],
                        'weight': 1,
                    }
                    sna = self.create_pathport_assoc(assco_args)
                    LOG.debug('create assoc src port with node: %s', sna)
                    src_node['portpair_details'].append(new_dst_pd['id'])

    def _create_src_and_dest_nodes(self, port_chain, next_group_intid,
                                   next_group_members, path_nodes):
        path_id = port_chain['chain_id']
        port_pair_groups = port_chain['port_pair_groups']
        sf_path_length = len(port_pair_groups)

        # Create a head node object for port chain
        src_args = {'tenant_id': port_chain['tenant_id'],
                    'node_type': ovs_const.SRC_NODE,
                    'nsp': path_id,
                    'nsi': 0xff,
                    'portchain_id': port_chain['id'],
                    'status': ovs_const.STATUS_BUILDING,
                    'next_group_id': next_group_intid,
                    'next_hop': jsonutils.dumps(next_group_members),
                    }
        src_node = self.create_path_node(src_args)
        LOG.debug('create src node: %s', src_node)
        path_nodes.append(src_node)

        # Create a destination node object for port chain
        dst_args = {
            'tenant_id': port_chain['tenant_id'],
            'node_type': ovs_const.DST_NODE,
            'nsp': path_id,
            'nsi': 0xff - sf_path_length - 1,
            'portchain_id': port_chain['id'],
            'status': ovs_const.STATUS_BUILDING,
            'next_group_id': None,
            'next_hop': None
        }
        dst_node = self.create_path_node(dst_args)
        LOG.debug('create dst node: %s', dst_node)
        path_nodes.append(dst_node)

        return src_node

    def _check_if_bi_node(self, src_node):
        ''' We assume that if the flow classifiers associated with
        a port chain contain both LSP and LDP, we treat it as a bi-directional
        chain. LSP and LDP can be part of same or different flow
        classifier(s) '''

        is_lsp = False
        is_ldp = False
        portpair_details = src_node['portpair_details']
        for each in portpair_details:
            port_detail = self.get_port_detail_by_filter(dict(id=each))
            if port_detail['egress']:
                is_lsp = True
            else:
                is_ldp = True
            if is_lsp and is_ldp:
                return True
        return False

    @log_helpers.log_method_call
    def _create_portchain_path(self, context, port_chain):
        path_nodes = []
        # Create an assoc object for chain_id and path_id
        # context = context._plugin_context
        path_id = port_chain['chain_id']

        if not path_id:
            LOG.error(_LE('No path_id available for creating port chain path'))
            return

        port_pair_groups = port_chain['port_pair_groups']
        sf_path_length = len(port_pair_groups)

        # Detect cross-subnet transit
        # Compare subnets for logical source ports
        # and first PPG ingress ports
        '''for fc in self._get_fcs_by_ids(port_chain['flow_classifiers']):
            if fc['logical_source_port']:
                subnet1 = self._get_subnet_by_port(fc['logical_source_port'])
            else:
                subnet1 = self._get_subnet_by_port(fc[
                                                'logical_destination_port'])
            cidr1 = subnet1['cidr']
            ppg = context._plugin.get_port_pair_group(context._plugin_context,
                                                      port_pair_groups[0])
            for pp_id1 in ppg['port_pairs']:
                pp1 = context._plugin.get_port_pair(context._plugin_context,
                                                    pp_id1)
                filter1 = {}
                if pp1.get('ingress', None):
                    filter1 = dict(dict(ingress=pp1['ingress']), **filter1)
                    pd1 = self.get_port_detail_by_filter(filter1)
                    subnet2 = self._get_subnet_by_port(pd1['ingress'])
                    cidr2 = subnet2['cidr']
                    if cidr1 != cidr2:
                        LOG.error(_LE('Cross-subnet chain not supported'))
                        raise exc.SfcDriverError()
                        return None'''

        # Compare subnets for PPG egress ports
        # and next PPG ingress ports
        for i in range(sf_path_length - 1):
            ppg = context._plugin.get_port_pair_group(context._plugin_context,
                                                      port_pair_groups[i])
            next_ppg = context._plugin.get_port_pair_group(
                context._plugin_context, port_pair_groups[i + 1])
            for pp_id1 in ppg['port_pairs']:
                pp1 = context._plugin.get_port_pair(context._plugin_context,
                                                    pp_id1)
                filter1 = {}
                if pp1.get('egress', None):
                    filter1 = dict(dict(egress=pp1['egress']), **filter1)
                    pd1 = self.get_port_detail_by_filter(filter1)
                    subnet1 = self._get_subnet_by_port(pd1['egress'])
                    cidr3 = subnet1['cidr']

                for pp_id2 in next_ppg['port_pairs']:
                    pp2 = context._plugin.get_port_pair(
                        context._plugin_context, pp_id2)
                    filter2 = {}
                    if pp2.get('ingress', None):
                        filter2 = dict(dict(ingress=pp2['ingress']), **filter2)
                        pd2 = self.get_port_detail_by_filter(filter2)
                        subnet2 = self._get_subnet_by_port(pd2['ingress'])
                        cidr4 = subnet2['cidr']
                        if cidr3 != cidr4:
                            LOG.error(_LE('Cross-subnet chain not supported'))
                            raise exc.SfcDriverError()
                            return None

        next_group_intid, next_group_members = self._get_portgroup_members(
            context, port_chain['port_pair_groups'][0])

        src_node = self._create_src_and_dest_nodes(
                                        port_chain, next_group_intid,
                                        next_group_members, path_nodes)

        self._add_flowclassifier_port_assoc(
            port_chain['flow_classifiers'],
            port_chain['tenant_id'],
            src_node
        )

        is_bi_node = self._check_if_bi_node(src_node)

        if is_bi_node:
            path_nodes[0].update(dict(reverse_path=True))
        else:
            path_nodes[0].update(dict(reverse_path=False))

        for i in range(sf_path_length):
            cur_group_members = next_group_members
            # next_group for next hop
            if i < sf_path_length - 1:
                next_group_intid, next_group_members = (
                    self._get_portgroup_members(
                        context, port_pair_groups[i + 1])
                )
            else:
                next_group_intid = None
                next_group_members = None

            # Create a node object
            node_args = {
                'tenant_id': port_chain['tenant_id'],
                'node_type': ovs_const.SF_NODE,
                'nsp': path_id,
                'nsi': 0xfe - i,
                'portchain_id': port_chain['id'],
                'status': ovs_const.STATUS_BUILDING,
                'next_group_id': next_group_intid,
                'next_hop': (
                    None if not next_group_members else
                    jsonutils.dumps(next_group_members)
                )
            }
            sf_node = self.create_path_node(node_args)
            LOG.debug('chain path node: %s', sf_node)

            # If Src Node is bi, then SF node shall naturally be bi.
            if is_bi_node:
                sf_node.update(dict(reverse_path=True))
            else:
                sf_node.update(dict(reverse_path=False))
            # Create the assocation objects that combine the pathnode_id with
            # the ingress of the port_pairs in the current group
            # when port_group does not reach tail
            for member in cur_group_members:
                assco_args = {'portpair_id': member['portpair_id'],
                              'pathnode_id': sf_node['id'],
                              'weight': member['weight'], }
                sfna = self.create_pathport_assoc(assco_args)
                LOG.debug('create assoc port with node: %s', sfna)
                sf_node['portpair_details'].append(member['portpair_id'])
            path_nodes.append(sf_node)

        return path_nodes

    def _delete_path_node_port_flowrule(self, node, port, fc_ids):
        # if this port is not binding, don't to generate flow rule
        if not port['host_id']:
            return
        flow_rule = self._build_portchain_flowrule_body(
            node,
            port,
            None,
            fc_ids)

        if flow_rule['reverse_path']:
            if (flow_rule['node_type'] == ovs_const.SRC_NODE and flow_rule[
                                                                'ingress']):
                flow_rule = self._reverse_flow_rules(flow_rule)

        LOG.info("ZZZ FLOW RULES %r" % flow_rule)
        self.ovs_driver_rpc.ask_agent_to_delete_flow_rules(
            self.admin_context,
            flow_rule)

        self._delete_agent_fdb_entries(flow_rule)

    def _delete_path_node_flowrule(self, node, fc_ids):
        if node['portpair_details'] is None:
            return
        for each in node['portpair_details']:
            port = self.get_port_detail_by_filter(dict(id=each))
            if port:
                if node['node_type'] == ovs_const.SF_NODE:
                    _, egress = self._get_ingress_egress_tap_ports(port)
                    port.update({'egress': egress})
                self._delete_path_node_port_flowrule(
                    node, port, fc_ids)

    @log_helpers.log_method_call
    def _delete_portchain_path(self, context, port_chain):
        pds = self.get_path_nodes_by_filter(
            dict(portchain_id=port_chain['id']))
        src_node = None
        is_bi_node = True
        if pds:
            for pd in pds:
                ''' We cannot assume that Src node always comes first.
                # If Src Node is bi, then SF node shall naturally be bi. Here,
                # we assume that Src node will be the first in 'pds'.
                if self._check_if_bi_node(pd):
                    is_bi_node = True'''
                if is_bi_node:
                    pd.update(dict(reverse_path=True))
                else:
                    pd.update(dict(reverse_path=False))

                if pd['node_type'] == ovs_const.SRC_NODE:
                    src_node = pd

                self._delete_path_node_flowrule(
                    pd,
                    port_chain['flow_classifiers']
                )
            for pd in pds:
                self.delete_path_node(pd['id'])

        # delete the ports on the traffic classifier
        self._remove_flowclassifier_port_assoc(
            port_chain['flow_classifiers'],
            port_chain['tenant_id'],
            src_node
        )

    def _filter_flow_classifiers(self, flow_rule, fc_ids):
        """Filter flow classifiers.

        @return: list of the flow classifiers
        """

        fc_return = []
        core_plugin = manager.NeutronManager.get_plugin()

        if not fc_ids:
            return fc_return
        fcs = self._get_fcs_by_ids(fc_ids)
        for fc in fcs:
            new_fc = fc.copy()
            new_fc.pop('id')
            new_fc.pop('name')
            new_fc.pop('tenant_id')
            new_fc.pop('description')

            lsp = new_fc.get('logical_source_port')
            ldp = new_fc.get('logical_destination_port')
            if lsp:
                port_detail = core_plugin.get_port(self.admin_context, lsp)
                new_fc['lsp_mac_address'] = port_detail['mac_address']
                new_fc['source_ip_prefix'] = port_detail[
                                            'fixed_ips'][0]['ip_address']
            if ldp:
                port_detail = core_plugin.get_port(self.admin_context, ldp)
                new_fc['ldp_mac_address'] = port_detail['mac_address']
                new_fc['destination_ip_prefix'] = port_detail[
                                            'fixed_ips'][0]['ip_address']

            if (
                flow_rule['node_type'] in [ovs_const.SRC_NODE] and
                flow_rule['egress'] == fc['logical_source_port']
            ) or (
                flow_rule['node_type'] in [ovs_const.SRC_NODE] and
                flow_rule['ingress'] == fc['logical_destination_port']
            ):
                fc_return.append(new_fc)
            elif flow_rule['node_type'] in [ovs_const.SF_NODE]:
                fc_return.append(new_fc)

        return fc_return

    def _reverse_flow_rules(self, flowrule):

        def _reverse_fcs(op):
            for fc in flowrule[op]:
                fc['logical_destination_port'], fc['logical_source_port'] = (
                    fc['logical_source_port'], fc['logical_destination_port'])
                fc['ldp_mac_address'], fc['lsp_mac_address'] = (
                    fc['lsp_mac_address'], fc['ldp_mac_address'])
                fc['destination_ip_prefix'], fc['source_ip_prefix'] = (
                    fc['source_ip_prefix'], fc['destination_ip_prefix'])

        for op in ['add_fcs', 'del_fcs']:
            _reverse_fcs(op)

        flowrule['ingress'], flowrule['egress'] = (
                    flowrule['egress'], flowrule['ingress'])

        return flowrule

    def _update_path_node_port_flowrules(self, node, port,
                                         add_fc_ids=None, del_fc_ids=None):
        # if this port is not binding, don't to generate flow rule
        if not port['host_id']:
            return

        flow_rule = self._build_portchain_flowrule_body(
            node,
            port,
            add_fc_ids,
            del_fc_ids)

        if flow_rule['reverse_path']:
            if (flow_rule['node_type'] == ovs_const.SRC_NODE and flow_rule[
                                                                'ingress']):
                flow_rule = self._reverse_flow_rules(flow_rule)

        LOG.info("YYY FLOW RULES %r" % flow_rule)
        self.ovs_driver_rpc.ask_agent_to_update_flow_rules(
            self.admin_context,
            flow_rule)

        self._update_agent_fdb_entries(flow_rule)

    def _update_path_node_flowrules(self, node,
                                    add_fc_ids=None, del_fc_ids=None):
        if node['portpair_details'] is None:
            return
        for each in node['portpair_details']:
            port = self.get_port_detail_by_filter(dict(id=each))

            if port:
                if node['node_type'] == ovs_const.SF_NODE:
                    _, egress = self._get_ingress_egress_tap_ports(port)
                    port.update({'egress': egress})
                self._update_path_node_port_flowrules(
                    node, port, add_fc_ids, del_fc_ids)

    @log_helpers.log_method_call
    def create_port_chain(self, context):
        port_chain = context.current
        path_nodes = self._create_portchain_path(context, port_chain)
        LOG.info("XXX PATH NODES %r" % path_nodes)
        self._update_path_nodes(
            path_nodes,
            port_chain['flow_classifiers'],
            None)

    @log_helpers.log_method_call
    def _get_portpair_detail_info(self, portpair_id):
        """Get port detail.

        @param: portpair_id: uuid
        @return: (host_id, local_ip, network_type, segment_id,
        service_insert_type): tuple
        """

        core_plugin = manager.NeutronManager.get_plugin()
        port_detail = core_plugin.get_port(self.admin_context, portpair_id)
        host_id, local_ip, network_type, segment_id, mac_address = (
            (None, ) * 5)

        if port_detail:
            host_id = port_detail['binding:host_id']
            network_id = port_detail['network_id']
            mac_address = port_detail['mac_address']
            network_info = core_plugin.get_network(
                self.admin_context, network_id)
            network_type = network_info['provider:network_type']
            segment_id = network_info['provider:segmentation_id']

        if network_type not in [np_const.TYPE_VXLAN, np_const.TYPE_VLAN]:
            LOG.warning(_LW("Currently only support vxlan and vlan networks"))
            return ((None, ) * 5)
        elif not host_id:
            LOG.warning(_LW("This port has not been binding"))
            return ((None, ) * 5)
        else:
            if network_type == np_const.TYPE_VXLAN:
                driver = core_plugin.type_manager.drivers.get(network_type)
                host_endpoint = driver.obj.get_endpoint_by_host(host_id)
                if host_endpoint:
                    local_ip = host_endpoint['ip_address']
                else:
                    local_ip = None
            else:
                local_ip = host_id

        return host_id, local_ip, network_type, segment_id, mac_address

    def _get_ingress_egress_tap_ports(self, port_pair):
        ingress_shadow_port_id = port_pair.get('ingress')
        egress_shadow_port_id = port_pair.get('egress')

        core_plugin = manager.NeutronManager.get_plugin()
        in_shadow_pd = core_plugin.get_port(self.admin_context,
                                            ingress_shadow_port_id)
        eg_shadow_pd = core_plugin.get_port(self.admin_context,
                                            egress_shadow_port_id)
        return in_shadow_pd['device_owner'], eg_shadow_pd['device_owner']

    def _get_shadow_port_segment_id(self, port):
        core_plugin = manager.NeutronManager.get_plugin()
        port_detail = core_plugin.get_port(self.admin_context, port)
        network_id = port_detail['network_id']
        network_info = core_plugin.get_network(
            self.admin_context, network_id)
        network_type = network_info['provider:network_type']
        segment_id = network_info['provider:segmentation_id']

        return network_type, segment_id

    @log_helpers.log_method_call
    def _create_port_detail(self, port_pair):
        # since first node may not assign the ingress port, and last node may
        # not assign the egress port. we use one of the
        # port as the key to get the SF information.
        port = None
        if port_pair.get('ingress', None):
            port = port_pair['ingress']
        elif port_pair.get('egress', None):
            port = port_pair['egress']

        host_id, local_endpoint, network_type, segment_id, mac_address = (
            self._get_portpair_detail_info(port))

        ingress, egress = port_pair.get('ingress'), port_pair.get('egress')

        port_detail = {
            'ingress': ingress,
            'egress': egress,
            'tenant_id': port_pair['tenant_id'],
            'host_id': host_id,
            'segment_id': segment_id,
            'network_type': network_type,
            'local_endpoint': local_endpoint,
            'mac_address': mac_address
        }
        r = self.create_port_detail(port_detail)
        LOG.debug('create port detail: %s', r)
        return r
