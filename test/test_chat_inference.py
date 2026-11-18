"""Exercise chat transport with fake model calls and installed ROS definitions."""
import json
from threading import RLock
from types import SimpleNamespace
import smpa_ros_interfaces.action as actions

# The source action definitions are newer than the installed ROS overlay.
if not hasattr(actions, 'DescribeStructuredImage'):
    actions.DescribeStructuredImage = actions.DescribeImage
if not hasattr(actions, 'GenerateStructuredChat'):
    actions.GenerateStructuredChat = actions.GenerateStructured
from smpa_llm.server import InferenceServer

class Goal:
    """Capture the outcome selected by the server for one fake request."""
    def __init__(self, **kwargs):
        """Keep request fields and start without a terminal outcome."""
        self.request = SimpleNamespace(**kwargs)
        self.outcome = None
    def succeed(self):
        """Record successful completion for the calling test."""
        self.outcome = 'succeeded'
    def abort(self):
        """Record failure for the calling test."""
        self.outcome = 'aborted'

class Server:
    """Provide the minimum state needed by the real inference callback."""
    def __init__(self, backend, kind):
        """Retain a fake backend, its catalogue kind and initial ready state.

        The real callback reads readiness before validating a request, so keep
        current state alongside the recorded transitions without loading weights.
        """
        self.lock = RLock()
        self.backend = backend
        self.active_alias = 'test'
        self.loaded_settings = {'backend': kind}
        self.busy = True
        self.state = 'ready'
        self.states = []
    def settings(self, alias):
        """Return fixed generation settings for the current request."""
        return dict(max_new_tokens=128, temperature=0.0, top_p=1.0, seed=-1)
    def change_state(self, state):
        """Update current readiness and retain each transition for assertions."""
        self.state = state
        self.states.append(state)
    def get_logger(self):
        """Accept errors without publishing ROS logging messages."""
        return SimpleNamespace(error=lambda message: None)

def test_role_preservation_and_schema():
    """Pass chat roles and a real schema unchanged to the existing backend.

    Check the returned JSON and exclusive-slot cleanup, then reject malformed
    roles before the fake backend is called a second time.
    """
    calls = []
    def generate(messages, **options):
        """Capture the exact messages and options while returning fixture JSON."""
        calls.append((messages, options))
        return '{"ok": true}'
    server = Server(SimpleNamespace(generate=generate), 'transformers')
    messages = [{'role': 'system', 'content': 'instruction'}, {'role': 'user', 'content': 'hello'}]
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
    goal = Goal(messages_json=json.dumps(messages), json_schema=json.dumps(schema))
    result = InferenceServer.infer(server, goal, action_type=actions.GenerateStructuredChat, result_field='json_output', chat=True)
    assert goal.outcome == 'succeeded' and json.loads(result.json_output) == {'ok': True}
    assert calls[0][0] == messages and calls[0][1]['schema'] == schema
    assert not server.busy
    invalid = Goal(messages_json='[{"role":"tool","content":"x"}]', json_schema='')
    InferenceServer.infer(server, invalid, action_type=actions.GenerateStructuredChat, result_field='json_output', chat=True)
    assert invalid.outcome == 'aborted' and len(calls) == 1

def test_api_chat_uses_role_preserving_entry_point():
    """Forward roles through the API conversation entry point without a schema."""
    calls = []
    def conversation(messages, **options):
        """Capture the message list and return the API's response wrapper."""
        calls.append((messages, options))
        return {'response': 'plain'}
    server = Server(SimpleNamespace(generate=lambda *a, **k: None, generate_conversation=conversation), 'api')
    messages = [{'role': 'assistant', 'content': 'previous'}]
    goal = Goal(messages_json=json.dumps(messages), json_schema='')
    result = InferenceServer.infer(server, goal, action_type=actions.GenerateStructuredChat, result_field='json_output', chat=True)
    assert goal.outcome == 'succeeded' and result.json_output == 'plain'
    assert calls[0][0] == messages and calls[0][1]['schema'] is None
