# Copyright 2016 Futurewei. All rights reserved.
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

from oslo_log import helpers as log_helpers

from networking_sfc.services.flowclassifier.common import exceptions as exc
from networking_sfc.services.flowclassifier.drivers.ovs import driver as fc_dvr


class OCFlowClassifierDriver(fc_dvr.OVSFlowClassifierDriver):
    """FlowClassifier Driver Base Class."""

    @log_helpers.log_method_call
    def create_flow_classifier_precommit(self, context):
        """OVS Driver precommit before transaction committed.

        Make sure that either the logical_source_port
        or the logical_destination_port is not None.
        """

        flow_classifier = context.current
        logical_source_port = flow_classifier['logical_source_port']
        logical_destination_port = flow_classifier['logical_destination_port']
        if (logical_source_port or logical_destination_port) is None:
            raise exc.FlowClassifierBadRequest(message=(
                'FlowClassifier %s requires either logical destination port or'
                ' logical source port in ovs driver' % flow_classifier['id']))
