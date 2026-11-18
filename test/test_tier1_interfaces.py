"""Verify that Tier 1 generated types retain the agreed minimal wire contracts."""
from pathlib import Path

import pytest
from rclpy.type_support import check_for_type_support
from smpa_ros_interfaces import action, msg, srv


ACTION_CONTRACTS = [
    ('GenerateText', ['prompt', 'run_id', 'stage'], ['text'], ['partial_text']),
    ('GenerateStructured', ['prompt', 'json_schema', 'run_id', 'stage'], ['json_output'], ['partial_text']),
    ('DescribeImage', ['image', 'prompt', 'run_id', 'stage'], ['description'], ['partial_text']),
    ('DescribeStructuredImage', ['image', 'prompt', 'json_schema', 'run_id', 'stage'], ['json_output'], ['partial_text']),
    ('GenerateStructuredChat', ['messages_json', 'json_schema', 'run_id', 'stage'], ['json_output'], []),
    ('Sense', ['run_id', 'capability', 'prompt_name'], ['observation', 'record_path'], []),
    ('Model', ['run_id', 'capability', 'prompt_name', 'observation', 'source_record', 'previous_snapshot', 'messages_json'],
     ['world_model', 'record_path', 'snapshot_path'], []),
    ('Plan', ['run_id', 'capability', 'prompt_name', 'world_model', 'source_record'], ['action_json', 'record_path'], []),
    ('Act', ['run_id', 'capability', 'prompt_name', 'action_json', 'source_record'], ['success', 'record_path'], []),
    ('LoadModel', ['model_id'], ['success', 'error_message'], ['stage']),
    ('UnloadModel', [], ['success', 'error_message'], ['stage']),
    ('SetActiveModel', ['model_id'], ['success', 'error_message'], ['stage']),
]


@pytest.mark.parametrize('name,goal,result,feedback', ACTION_CONTRACTS)
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


def test_source_action_contracts_are_registered():
    """Check the approved action fields and registration directly from source.

    Read each action definition in the checkout, separate its goal, result and
    feedback sections, and compare their field names with the same contracts
    used for generated types. Require every action to appear in CMake as well.
    This check can run before rebuilding, but does not prove ROS type support.
    """
    root = Path(__file__).resolve().parents[1] / 'smpa_ros_interfaces'
    registration = (root / 'CMakeLists.txt').read_text()
    for name, goal, result, feedback in ACTION_CONTRACTS:
        relative = f'action/{name}.action'
        assert f'"{relative}"' in registration
        sections = (root / relative).read_text().split('---')
        assert len(sections) == 3
        fields = [[line.split()[1] for line in section.splitlines()
                   if line.strip() and not line.lstrip().startswith('#')]
                  for section in sections]
        assert fields == [goal, result, feedback], name
