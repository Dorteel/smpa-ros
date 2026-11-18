"""Verify that Tier 1 generated types retain the agreed minimal wire contracts."""
import pytest
from rclpy.type_support import check_for_type_support
from smpa_ros_interfaces import action, msg, srv


@pytest.mark.parametrize('name,goal,result,feedback', [
    ('GenerateText', ['prompt'], ['text'], ['partial_text']),
    ('GenerateStructured', ['prompt', 'json_schema'], ['json_output'], ['partial_text']),
    ('DescribeImage', ['image', 'prompt'], ['description'], ['partial_text']),
    ('LoadModel', ['model_id'], ['success', 'error_message'], ['stage']),
    ('UnloadModel', [], ['success', 'error_message'], ['stage']),
    ('SetActiveModel', ['model_id'], ['success', 'error_message'], ['stage']),
])
def test_action_contract(name, goal, result, feedback):
    """Inspect each installed action without starting any server or model.

    Compare goal, result and feedback fields to the agreed wire contract and
    require generated ROS type support. Generation controls must not leak into
    requests as a side effect of implementing model-specific parameters.
    """
    interface = getattr(action, name)
    check_for_type_support(interface)
    for component, fields in [(interface.Goal, goal), (interface.Result, result),
                              (interface.Feedback, feedback)]:
        assert list(component.get_fields_and_field_types()) == fields


@pytest.mark.parametrize('name,request_fields,response', [
    ('ListModels', [], ['model_ids']),
    ('GetModelStatus', [], ['active_model', 'state']),
    ('GetSmpaStatus', [], ['state', 'run_id', 'active_stage']),
    ('StartSmpa', ['goal'], ['accepted', 'run_id']),
    ('StopSmpa', [], ['accepted']),
])
def test_service_contract(name, request_fields, response):
    """Require generated services to expose only their agreed request and response.

    Check type support as well as field names so a source definition without
    a corresponding generated installation cannot satisfy this test.
    """
    interface = getattr(srv, name)
    check_for_type_support(interface)
    assert list(interface.Request.get_fields_and_field_types()) == request_fields
    assert list(interface.Response.get_fields_and_field_types()) == response


@pytest.mark.parametrize('name,fields', [('InferenceStatus', ['state', 'active_model']),
                                        ('SmpaEvent', ['run_id', 'stage', 'event'])])
def test_notification_contract(name, fields):
    """Require the two notification messages to retain minimal state information.

    Resolve their installed type support and compare fields without publishing
    messages or changing the existing persistent records they describe.
    """
    interface = getattr(msg, name)
    check_for_type_support(interface)
    assert list(interface.get_fields_and_field_types()) == fields
