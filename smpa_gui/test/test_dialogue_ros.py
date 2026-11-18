"""Integration tests of the Dialogue backend over ROS 2, against lightweight stand-ins for SMPA's nodes.

The stand-ins (``fixtures/cognitive_nodes.py``) use SMPA's real interface
package ``smpa_ros_interfaces`` and run no inference. The four stage
stand-ins are test-only: the real stage interfaces are being defined by
Codex, so the layout written here configures the coordinator's ROS stage
adapters with existing types purely to exercise the ROS calls. The tests
are skipped when ``smpa_ros_interfaces`` is not installed (source the SMPA
workspace).
"""
import asyncio
import json
import subprocess
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from conftest import GUI, PYTHON, fetch, isolated_environment, start_server, stop

API = '/api/v1'
ROBOT_RUN = str(uuid.UUID(int=1))
DIALOGUE_RUN = str(uuid.UUID(int=2))
OTHER_RUN = str(uuid.UUID(int=3))


def interfaces_installed():
    """Tell whether the GUI's interpreter can import SMPA's interface package."""
    return subprocess.run([PYTHON, '-c', 'import smpa_ros_interfaces.action'], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not interfaces_installed(),
                                reason='smpa_ros_interfaces is not installed (source the SMPA workspace)')

STAGES = {
    'sense': dict(kind='action', endpoint='/fixture_dialogue/sense', type='smpa_ros_interfaces/action/GenerateText',
                  request={'prompt': '{message}'}, outputs={'observation_id': 'text'}, timeout_s=10),
    'model': dict(kind='action', endpoint='/fixture_dialogue/model', type='smpa_ros_interfaces/action/GenerateStructured',
                  request={'prompt': '{belief_context_id}|{observation_id}', 'json_schema': '{message}'},
                  outputs={'model_invocation_id': 'json_output'}, timeout_s=10),
    'plan': dict(kind='action', endpoint='/fixture_dialogue/plan', type='smpa_ros_interfaces/action/GenerateText',
                 request={'prompt': '{message}'}, outputs={'response': 'text'}, timeout_s=10),
    'act': dict(kind='action', endpoint='/fixture_dialogue/act', type='smpa_ros_interfaces/action/Talk',
                request={'text': '{response}'}, outputs={}, success='success', error='error_message', timeout_s=10),
}


def write_memory(directory):
    """Create a memory directory with three runs (robot, dialogue, other), each with metadata and a belief file."""
    for number, run_id in enumerate((ROBOT_RUN, DIALOGUE_RUN, OTHER_RUN)):
        run = directory / 'runs' / f'2026-11-18_12000{number}_000000+0000_test_{run_id}'
        run.mkdir(parents=True)
        (run / 'metadata.yaml').write_text(f'run_id: {run_id}\nrun_name: test\nstarted_at: 2026-11-18T12:00:0{number}+00:00\n')
        (run / 'belief-state.ttl').write_text('@prefix ex: <http://example.org/> .\nex:start ex:in ex:run .\n')
    return directory


def start_stand_ins(environment, memory, *arguments):
    """Start the stand-in nodes and wait until they say every endpoint exists."""
    process = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/cognitive_nodes.py'), '--memory', str(memory),
                                '--run-id', ROBOT_RUN, *arguments], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = process.stdout.readline()
    if 'COGNITIVE READY' not in line:
        process.kill()
        raise RuntimeError('Stand-in nodes failed: ' + line + process.stdout.read())
    return process


def wait_until(condition, timeout=30.0, message='condition was not reached'):
    """Poll a condition until it holds, failing with a message after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(message)


def get(base, path, **parameters):
    """Fetch one JSON route of the GUI."""
    return fetch(base + API + path + ('?' + urlencode(parameters) if parameters else ''), timeout=15)


def post(base, path, body, token):
    """POST JSON with the session token; return (status, body)."""
    request = Request(base + API + path, data=json.dumps(body).encode(), method='POST',
                      headers={'Content-Type': 'application/json', 'X-SMPA-GUI-Token': token})
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def finished(base, turn_id, timeout=30.0):
    """Wait until a turn is no longer running and return its record."""
    record = {}

    def done():
        """Read the turn and tell whether it ended."""
        record.update(get(base, f'/smpa/dialogue/turns/{turn_id}')['turn'])
        return record['state'] != 'running'
    wait_until(done, timeout, 'the turn did not finish')
    return record


@pytest.fixture(scope='module')
def world(ros_environment, tmp_path_factory):
    """Run the stand-ins and a GUI whose layout connects the four stages to them; yield what the tests need."""
    import yaml
    folder = tmp_path_factory.mktemp('dialogue')
    memory = write_memory(folder / 'memory')
    layout = folder / 'layout.yaml'
    layout.write_text(yaml.safe_dump(dict(dialogue=dict(context='independent', stages=STAGES))))
    environment = dict(ros_environment, SMPA_GUI_MEMORY_ROOT=str(memory), SMPA_GUI_LAYOUT=str(layout))
    stand_ins = start_stand_ins(environment, memory)
    process, base = start_server(environment)
    token = get(base, '/session')['token']
    wait_until(lambda: get(base, '/smpa/ros')['robot']['state'] == 'ok', message='run control never answered')
    yield dict(base=base, token=token, memory=memory, environment=environment, stand_ins=stand_ins, process=process)
    stop(process)
    stop(stand_ins)


def calls(world):
    """Ask the stand-ins which goals each stage received."""
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture_dialogue/calls', type='std_srvs/srv/Trigger', request={},
                             request_id=f'calls{time.monotonic_ns()}'), world['token'])
    assert status == 200, body
    return json.loads(body['call']['response']['message'])


def context(world, mode, **identifiers):
    """Choose the Dialogue's belief context."""
    status, body = post(world['base'], '/smpa/dialogue/context', dict(mode=mode, **identifiers), world['token'])
    assert status == 200, body
    return body['context']


def turn(world, text):
    """Start a turn and return its record as answered at once."""
    status, body = post(world['base'], '/smpa/dialogue/turns', dict(text=text, request_id=f'turn{time.monotonic_ns()}'),
                        world['token'])
    assert status == 202, body
    return body['turn']


# ----- existing SMPA interfaces ---------------------------------------------------------------------

def test_existing_run_control_and_inference_interfaces_are_read(world):
    """GetSmpaStatus, ListModels and GetModelStatus are called with SMPA's real types and reported."""
    answer = get(world['base'], '/smpa/ros')
    assert answer['robot'] == dict(state='ok', endpoint='/smpa/get_status', run_state='running', run_id=ROBOT_RUN,
                                   active_stage='model')
    assert answer['inference']['models'] == dict(state='ok', endpoint='/llm/list_models', model_ids=['stub-a', 'stub-b'])
    assert answer['inference']['status'] == dict(state='ok', endpoint='/llm/get_model_status', server_state='ready',
                                                 active_model='stub-a')
    assert answer['endpoints']['smpa_events'] == '/smpa/events'


def test_missing_interfaces_are_reported_not_fatal(ros_environment, tmp_path):
    """Without SMPA's nodes the status says unavailable within about a second per service, and the Dialogue is pending."""
    environment = dict(isolated_environment(), SMPA_GUI_MEMORY_ROOT=str(write_memory(tmp_path / 'memory')))  # an empty ROS domain
    process, base = start_server(environment)
    try:
        started = time.monotonic()
        answer = get(base, '/smpa/ros')
        assert time.monotonic() - started < 5
        assert answer['robot']['state'] == 'unavailable' and answer['inference']['models']['state'] == 'unavailable'
        status = get(base, '/smpa/dialogue')['dialogue']
        assert status['path'] == 'conversation_server' and status['pending_stages'] == ['sense', 'model', 'plan', 'act']
        token = get(base, '/session')['token']
        refused = post(base, '/smpa/dialogue/turns', dict(text='hello', request_id='pending-0001'), token)
        assert refused[0] == 409 and 'pending stage interfaces: sense, model, plan, act' in refused[1]['detail']
    finally:
        stop(process)


# ----- sequencing, context and belief snapshots ------------------------------------------------------

def test_turn_runs_the_four_stages_in_order_and_associates_their_outputs(world):
    """Sense, Model, Plan and Act are called once each, in order, with the turn's values; outputs stay associated."""
    context(world, 'independent', session_id=DIALOGUE_RUN)
    before = calls(world)
    record = finished(world['base'], turn(world, 'Where is the cup?')['turn_id'])
    assert record['state'] == 'completed', record
    after = calls(world)
    assert [len(after[stage]) - len(before[stage]) for stage in ('sense', 'model', 'plan', 'act')] == [1, 1, 1, 1]
    observation = record['stages']['sense']['outputs']['observation_id']
    assert after['sense'][-1] == dict(prompt='Where is the cup?')
    assert after['model'][-1]['prompt'] == f'{DIALOGUE_RUN}|{observation}'
    assert after['act'][-1] == dict(text='reply to: Where is the cup?')
    values = record['values']
    assert values['response'] == 'reply to: Where is the cup?' and values['observation_id'] == observation
    assert values['model_invocation_id'].startswith('inv-')
    assert [record['stages'][stage]['state'] for stage in ('sense', 'model', 'plan', 'act')] == ['succeeded'] * 4
    starts = [record['stages'][stage]['started_at'] for stage in ('sense', 'model', 'plan', 'act')]
    assert starts == sorted(starts)


def test_belief_snapshot_is_read_after_model_commits(world):
    """The snapshot in the turn is the run's belief file as Model left it, the same revision the graph view reads."""
    context(world, 'independent', session_id=DIALOGUE_RUN)
    record = finished(world['base'], turn(world, 'Note the plate')['turn_id'])
    graph = get(world['base'], '/smpa/graphs/belief', session_id=DIALOGUE_RUN)
    assert record['belief']['revision'] == graph['revision'] and record['belief']['changed'] is True
    assert record['belief']['belief_context_id'] == DIALOGUE_RUN
    assert record['belief']['model_invocation_id'] == record['values']['model_invocation_id']
    assert record['belief_before']['revision'] != record['belief']['revision']


def test_shared_and_independent_contexts_carry_the_right_identifiers(world):
    """Shared uses the robot's run from run control (or a chosen run); independent uses the Dialogue's own session."""
    context(world, 'shared')
    shared = finished(world['base'], turn(world, 'Shared question')['turn_id'])
    assert shared['state'] == 'completed', shared
    assert shared['values']['context_mode'] == 'shared'
    assert shared['values']['run_id'] == ROBOT_RUN and shared['values']['belief_context_id'] == ROBOT_RUN
    assert calls(world)['model'][-1]['prompt'].startswith(ROBOT_RUN + '|')
    robot_belief = next((world['memory'] / 'runs').glob(f'*{ROBOT_RUN}')) / 'belief-state.ttl'
    assert shared['values']['observation_id'].replace('-', '_') in robot_belief.read_text()
    context(world, 'shared', run_id=OTHER_RUN)
    chosen = finished(world['base'], turn(world, 'Chosen run')['turn_id'])
    assert chosen['values']['belief_context_id'] == OTHER_RUN
    context(world, 'independent', session_id=DIALOGUE_RUN)
    independent = finished(world['base'], turn(world, 'Own beliefs')['turn_id'])
    assert independent['values']['belief_context_id'] == DIALOGUE_RUN == independent['values']['session_id']
    status, body = post(world['base'], '/smpa/dialogue/context', dict(mode='everything'), world['token'])
    assert status == 422


# ----- failures and recovery --------------------------------------------------------------------------

def test_failed_stage_stops_the_turn_and_a_retry_resumes_from_it(world):
    """A failing Plan stops before Act; the retry calls only Plan and Act again and completes."""
    context(world, 'independent', session_id=DIALOGUE_RUN)
    before = calls(world)
    failed = finished(world['base'], turn(world, 'please fail-plan once')['turn_id'])
    assert failed['state'] == 'failed' and failed['failed_stage'] == 'plan'
    assert failed['stages']['plan']['state'] == 'aborted' and failed['stages']['act']['state'] == 'waiting'
    assert failed['belief']['changed'] is True  # Model had committed before Plan failed
    middle = calls(world)
    assert len(middle['act']) == len(before['act'])
    status, body = post(world['base'], f"/smpa/dialogue/turns/{failed['turn_id']}/retry",
                        dict(request_id=f'retry{time.monotonic_ns()}'), world['token'])
    assert status == 202, body
    retried = finished(world['base'], body['turn']['turn_id'])
    assert retried['state'] == 'completed' and retried['retry_of'] == failed['turn_id']
    assert retried['stages']['sense']['state'] == 'skipped' and retried['stages']['model']['state'] == 'skipped'
    after = calls(world)
    assert [len(after[stage]) - len(middle[stage]) for stage in ('sense', 'model', 'plan', 'act')] == [0, 0, 1, 1]
    assert retried['values']['observation_id'] == failed['values']['observation_id']
    status, body = post(world['base'], f"/smpa/dialogue/turns/{retried['turn_id']}/retry",
                        dict(request_id=f'retry{time.monotonic_ns()}'), world['token'])
    assert status == 409


def test_model_failure_publishes_no_belief_snapshot(world):
    """When Model fails, the belief file is untouched and the turn holds no snapshot."""
    context(world, 'independent', session_id=DIALOGUE_RUN)
    revision = get(world['base'], '/smpa/graphs/belief', session_id=DIALOGUE_RUN)['revision']
    failed = finished(world['base'], turn(world, 'fail-model now')['turn_id'])
    assert failed['failed_stage'] == 'model' and failed['belief'] is None
    assert get(world['base'], '/smpa/graphs/belief', session_id=DIALOGUE_RUN)['revision'] == revision


def test_act_reporting_failure_is_reported_with_its_reason(world):
    """An Act result with success false fails the turn with the stage's own reason; the next turn works."""
    context(world, 'independent', session_id=DIALOGUE_RUN)
    failed = finished(world['base'], turn(world, 'say fail-act')['turn_id'])
    assert failed['failed_stage'] == 'act' and 'speaker unplugged' in failed['error']
    assert finished(world['base'], turn(world, 'and now normally')['turn_id'])['state'] == 'completed'


def test_unavailable_stage_and_missing_context_are_explained(ros_environment, tmp_path):
    """A stage whose server is not running fails as unavailable; the shared context without run control is refused."""
    import yaml
    memory = write_memory(tmp_path / 'memory')
    layout = tmp_path / 'layout.yaml'
    layout.write_text(yaml.safe_dump(dict(dialogue=dict(context='shared', stages=STAGES))))
    environment = dict(isolated_environment(), SMPA_GUI_MEMORY_ROOT=str(memory), SMPA_GUI_LAYOUT=str(layout))
    process, base = start_server(environment)  # in an empty ROS domain: no run control, no stage servers
    try:
        token = get(base, '/session')['token']
        status, body = post(base, '/smpa/dialogue/turns', dict(text='hello', request_id='nocontrol-01'), token)
        assert status == 409 and 'run control' in body['detail']
        post(base, '/smpa/dialogue/context', dict(mode='independent', session_id=DIALOGUE_RUN), token)
        status, body = post(base, '/smpa/dialogue/turns', dict(text='hello', request_id='nostages-01'), token)
        record = finished(base, body['turn']['turn_id'])
        assert record['failed_stage'] == 'sense' and record['stages']['sense']['state'] == 'unavailable'
    finally:
        stop(process)


def test_turn_requests_follow_the_change_rules(world):
    """A turn needs the session token; a repeated request id returns the same turn; one turn runs at a time."""
    status, _ = post(world['base'], '/smpa/dialogue/turns', dict(text='x', request_id='notoken-0001'), 'wrong')
    assert status == 403
    context(world, 'independent', session_id=DIALOGUE_RUN)
    first = post(world['base'], '/smpa/dialogue/turns', dict(text='slow thinking', request_id='same-request-01'), world['token'])
    again = post(world['base'], '/smpa/dialogue/turns', dict(text='slow thinking', request_id='same-request-01'), world['token'])
    assert again[1]['turn']['turn_id'] == first[1]['turn']['turn_id'] and again[1]['turn']['duplicate'] is True
    busy = post(world['base'], '/smpa/dialogue/turns', dict(text='other', request_id='busy-request-01'), world['token'])
    assert busy[0] == 409 and 'still running' in busy[1]['detail']
    finished(world['base'], first[1]['turn']['turn_id'])


# ----- nonblocking execution and the WebSocket ---------------------------------------------------------

def test_a_running_turn_does_not_block_other_requests(world):
    """While Sense takes two seconds, other routes answer at once."""
    context(world, 'independent', session_id=DIALOGUE_RUN)
    started = turn(world, 'slow observation')
    durations = []
    for _ in range(5):
        moment = time.monotonic()
        get(world['base'], '/health')
        assert get(world['base'], '/smpa/dialogue')['dialogue']['active_turn'] == started['turn_id']
        durations.append(time.monotonic() - moment)
        time.sleep(0.2)
    assert max(durations) < 0.5, durations
    assert finished(world['base'], started['turn_id'])['state'] == 'completed'


def test_stream_carries_turn_events_and_relays_smpa_topics(world):
    """The Dialogue stream sends a status, the turn's events in order, the belief snapshot, and SMPA's relayed topics."""
    import websockets
    context(world, 'independent', session_id=DIALOGUE_RUN)
    url = world['base'].replace('http', 'ws') + API + '/smpa/dialogue/stream'

    async def scenario():
        """Open the stream, start a turn, and collect messages until the turn completed and topics were relayed."""
        received = []
        async with websockets.connect(url, origin=world['base']) as socket:
            first = json.loads(await socket.recv())
            assert first['kind'] == 'status' and first['state'] == 'ok' and first['relays_unavailable'] == []
            identity = (await asyncio.to_thread(turn, world, 'Stream this'))['turn_id']
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(socket.recv(), 10))
                received.append(message)
                events = {item.get('event') for item in received}
                if any(item.get('event') == 'turn_completed' and item.get('turn_id') == identity for item in received) \
                        and {'smpa_event', 'inference_status'} <= events:
                    break
        return identity, received

    identity, received = asyncio.run(scenario())
    mine = [(item['event'], item.get('stage')) for item in received if item.get('turn_id') == identity]
    assert mine[0] == ('turn_started', None) and mine[-1] == ('turn_completed', None)
    order = [stage for event, stage in mine if event == 'stage_started']
    assert order == ['sense', 'model', 'plan', 'act']
    events = [event for event, _ in mine]
    assert events.index('belief_snapshot') > events.index('stage_completed') and \
        mine[events.index('belief_snapshot') - 1] == ('stage_completed', 'model')
    assert ('stage_feedback', 'act') in mine
    smpa = next(item for item in received if item.get('event') == 'smpa_event')
    assert smpa['kind'] == 'data' and smpa['message'] == dict(run_id=ROBOT_RUN, stage='model', event='stage_started')
    inference = next(item for item in received if item.get('event') == 'inference_status')
    assert inference['message'] == dict(state='ready', active_model='stub-a')


def test_stream_subscriptions_end_with_the_stream(world):
    """After the stream closes, the GUI holds no relay subscription to SMPA's topics."""
    import websockets
    url = world['base'].replace('http', 'ws') + API + '/smpa/dialogue/stream'

    async def open_and_close():
        """Open the stream, read its status, and close it."""
        async with websockets.connect(url, origin=world['base']) as socket:
            await socket.recv()

    asyncio.run(open_and_close())

    def released():
        """Tell whether the GUI node no longer subscribes to SMPA's event topic."""
        graph = get(world['base'], '/ros/graph')
        return not any(edge['kind'] == 'subscribes' and edge['source'] == 'topic:/smpa/events'
                       and edge['target'] == 'node:/smpa_gui' for edge in graph['edges'])
    wait_until(released, 10, 'the relay subscription was not released')


def test_read_only_and_disabled_guis_refuse_dialogue_changes(tmp_path):
    """A read-only GUI refuses turns and context changes; a GUI without SMPA answers 404 and starts no relay."""
    memory = write_memory(tmp_path / 'memory')
    for flag, expected in (('SMPA_GUI_READ_ONLY', 403), ('SMPA_GUI_NO_SMPA', 404)):
        process, base = start_server(dict(isolated_environment(), SMPA_GUI_MEMORY_ROOT=str(memory), **{flag: '1'}))
        try:
            token = get(base, '/session')['token'] or 'none'
            assert post(base, '/smpa/dialogue/turns', dict(text='hi', request_id='refused-0001'), token)[0] == expected
            assert post(base, '/smpa/dialogue/context', dict(mode='shared'), token)[0] == expected
            if expected == 404:
                with pytest.raises(HTTPError):
                    get(base, '/smpa/ros')
        finally:
            stop(process)
