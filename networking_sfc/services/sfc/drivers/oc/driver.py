from oslo_log import helpers as log_helpers

from networking_sfc.services.flowclassifier.common import exceptions as exc
from networking_sfc.services.flowclassifier.drivers import base as fc_driver


class OVSFlowClassifierDriver(fc_driver.FlowClassifierDriverBase):
    """FlowClassifier Driver Base Class."""

    def initialize(self):
        pass

    @log_helpers.log_method_call
    def create_flow_classifier(self, context):
        pass

    @log_helpers.log_method_call
    def update_flow_classifier(self, context):
        pass

    @log_helpers.log_method_call
    def delete_flow_classifier(self, context):
        pass

    @log_helpers.log_method_call
    def create_flow_classifier_precommit(self, context):
        """OVS Driver precommit before transaction committed.

        Make sure the logical_source_port is not None.
        Make sure the logical_destination_port is None.
        """
        flow_classifier = context.current
        logical_source_port = flow_classifier['logical_source_port']
        if logical_source_port is None:
            raise exc.FlowClassifierBadRequest(message=(
                'FlowClassifier %s does not set '
                'logical source port in ovs driver' % flow_classifier['id']))
        logical_destination_port = flow_classifier['logical_destination_port']
        if logical_destination_port is not None:
            raise exc.FlowClassifierBadRequest(message=(
                'FlowClassifier %s sets logical destination port '
                'in ovs driver' % flow_classifier['id']))
