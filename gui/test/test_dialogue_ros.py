"""Integration tests of the Dialogue's ROS 2 side against stand-ins for SMPA's participants.

The stand-ins (``fixtures/cognitive_nodes.py``) serve SMPA's real endpoint
names with SMPA's generated interface types (``smpa_ros_interfaces``): run
control, the inference server, and the four cognitive actions
``/smpa/sense``, ``/smpa/model``, ``/smpa/plan`` and ``/smpa/act``, which
persist records the way SMPA's stages do. No inference runs. The tests are
skipped when ``smpa_ros_interfaces`` is not installed (source the SMPA
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
    """Tell whether the GUI's interpreter can import SMPA's interface package with the merged stage actions."""
    check = 'from smpa_ros_interfaces.action import Sense, Model, Plan, Act'
    return subprocess.run([PYTHON, '-c', check], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not interfaces_installed(),
                                reason='smpa_ros_interfaces with the cognitive stage actions is not installed '
                                       '(source the SMPA workspace)')


def write_memory(directory):
    """Create a memory directory with a running robot run, a conversation run and a completed robot run.

    The metadata follows SMPA's MemoryManager (run_id, status, completed
    cycles, configuration); the robot runs have ``parameters.yaml`` and an
    active first cycle, as SMPA's experiment leaves them.
    """
    import yaml
    for number, (run_id, name, status) in enumerate(((ROBOT_RUN, 'smpa', 'running'), (DIALOGUE_RUN, 'conversation', 'running'),
                                                      (OTHER_RUN, 'smpa', 'completed'))):
        run = directory / 'runs' / f'2026-11-18_12000{number}_000000+0000_{name}_{run_id}'
        (run / 'state' / 'cycle_000001').mkdir(parents=True)
        (run / 'world_model').mkdir()
        (run / 'observations').mkdir()
        (run / 'metadata.yaml').write_text(yaml.safe_dump(dict(
            run_id=run_id, run_name=name, started_at=f'2026-11-18T12:00:0{number}+00:00', status=status,
            completed_cycles=0, configuration=dict(kind='conversation' if name == 'conversation' else 'experiment',
                                                   pipeline={}))))
        if name == 'smpa':
            (run / 'parameters.yaml').write_text(yaml.safe_dump(dict(stage_prompts={})))
            (run / 'goal.md').write_text('# Goal\n\nFixture goal.\n')
        (run / 'belief-state.ttl').write_text('@prefix ex: <http://example.org/> .\nex:start ex:in ex:run .\n')
    return directory


def start_stand_ins(environment, memory):
    """Start the stand-in nodes and wait until they say every endpoint exists."""
    process = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/cognitive_nodes.py'), '--memory', str(memory),
                                '--run-id', ROBOT_RUN], env=environment,
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
        with urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def rid(label):
    """Make a fresh request id."""
    return f'{label}{time.monotonic_ns()}'


def turn_state(base, turn_id, *states, timeout=30.0):
    """Wait until a turn reaches one of the given states and return its record."""
    record = {}

    def reached():
        """Read the turn and tell whether it is in a wanted state."""
        record.update(get(base, f'/smpa/dialogue/turns/{turn_id}')['turn'])
        return record['state'] in states
    wait_until(reached, timeout, f'the turn never reached {states}: {record}')
    return record


@pytest.fixture(scope='module')
def world(ros_environment, tmp_path_factory):
    """Run the stand-ins and a GUI reading the same memory directory; yield what the tests need."""
    folder = tmp_path_factory.mktemp('dialogue')
    memory = write_memory(folder / 'memory')
    environment = dict(ros_environment, SMPA_GUI_MEMORY_ROOT=str(memory))
    stand_ins = start_stand_ins(environment, memory)
    process, base = start_server(environment)
    token = get(base, '/session')['token']
    wait_until(lambda: get(base, '/smpa/ros')['robot']['state'] == 'ok'
               and all(stage['available'] for stage in get(base, '/smpa/ros')['stages'].values()),
               message='the stand-ins never became visible')
    yield dict(base=base, token=token, memory=memory, environment=environment, stand_ins=stand_ins, process=process)
    stop(process)
    stop(stand_ins)


def calls(world):
    """Ask the stand-ins which goals each endpoint received."""
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture_dialogue/calls', type='std_srvs/srv/Trigger', request={},
                             request_id=rid('calls')), world['token'])
    assert status == 200, body
    return json.loads(body['call']['response']['message'])


def context(world, mode, **identifiers):
    """Choose the Dialogue's context."""
    status, body = post(world['base'], '/smpa/dialogue/context', dict(mode=mode, **identifiers), world['token'])
    assert status == 200, body
    return body['context']


def step(world, note=''):
    """Start a cognitive step and return its record as answered at once."""
    status, body = post(world['base'], '/smpa/dialogue/turns', dict(text=note, request_id=rid('turn')), world['token'])
    assert status == 202, body
    return body['turn']


def decide(world, turn_id, approve):
    """Approve or decline the Act step of a turn."""
    return post(world['base'], f'/smpa/dialogue/turns/{turn_id}/act', dict(approve=approve, request_id=rid('act')),
                world['token'])


def robot_run(world):
    """Return the robot run's directory."""
    return next((world['memory'] / 'runs').glob(f'*_{ROBOT_RUN}'))


# ----- SMPA's existing interfaces ---------------------------------------------------------------------

def test_status_reports_run_control_inference_and_live_stage_servers(world):
    """GetSmpaStatus, ListModels and GetModelStatus are read with SMPA's types; the four stage servers are live."""
    answer = get(world['base'], '/smpa/ros')
    assert answer['robot'] == dict(state='ok', endpoint='/smpa/get_status', run_state='running', run_id=ROBOT_RUN,
                                   active_stage='model')
    assert answer['inference']['models']['model_ids'] == ['stub-a', 'stub-b']
    assert answer['inference']['status']['server_state'] == 'ready'
    assert {stage: (item['endpoint'], item['type'], item['available']) for stage, item in answer['stages'].items()} == {
        stage: (f'/smpa/{stage}', f'smpa_ros_interfaces/action/{stage.title()}', True) for stage in ('sense', 'model', 'plan', 'act')}
    assert answer['live'] is True
    status = get(world['base'], '/smpa/dialogue')['dialogue']
    assert status['ros_path_ready'] is True and status['pending_stages'] == [] and status['confirm_act'] is True


def test_independent_context_and_missing_run_explain_why_no_step_runs(world, tmp_path):
    """Steps need a running robot run: the independent context and a domain without run control are refused (409)."""
    context(world, 'independent')
    assert get(world['base'], '/smpa/dialogue')['dialogue']['steps'] == 'independent_context'
    status, body = post(world['base'], '/smpa/dialogue/turns', dict(request_id=rid('indep')), world['token'])
    assert status == 409 and 'independent context' in body['detail']
    environment = dict(isolated_environment(), SMPA_GUI_MEMORY_ROOT=str(write_memory(tmp_path / 'memory')))
    process, base = start_server(environment)  # an empty ROS domain: no run control, no stages
    try:
        token = get(base, '/session')['token']
        answer = get(base, '/smpa/ros')
        assert answer['robot']['state'] == 'unavailable' and not any(item['available'] for item in answer['stages'].values())
        post(base, '/smpa/dialogue/context', dict(mode='shared'), token)
        status, body = post(base, '/smpa/dialogue/turns', dict(request_id=rid('norun')), token)
        assert status == 409 and 'no running run' in body['detail']
        post(base, '/smpa/dialogue/context', dict(mode='shared', run_id=ROBOT_RUN), token)
        status, body = post(base, '/smpa/dialogue/turns', dict(request_id=rid('nostage')), token)
        record = turn_state(base, body['turn']['turn_id'], 'failed')
        assert record['failed_stage'] == 'sense' and record['stages']['sense']['state'] == 'unavailable'
    finally:
        stop(process)


# ----- cognitive steps over SMPA's stage actions ------------------------------------------------------

def test_step_calls_the_four_actions_with_smpa_contracts_and_waits_for_act_approval(world):
    """Sense, Model and Plan run with SMPA's goal fields; Act waits for approval, then runs; records stay linked."""
    context(world, 'shared')
    before = calls(world)
    started = step(world, 'Look at the card')
    waiting = turn_state(world['base'], started['turn_id'], 'awaiting_act')
    assert waiting['act_decision'] == dict(action='observe', target='green card', question='What colour is the card?')
    middle = calls(world)
    assert len(middle['act']) == len(before['act'])  # nothing acted before the approval
    sense, model, plan = middle['sense'][-1], middle['model'][-1], middle['plan'][-1]
    values = waiting['values']
    assert sense == dict(run_id=ROBOT_RUN, capability='', prompt_name='')
    assert model['run_id'] == ROBOT_RUN and model['observation'] == values['observation']
    assert model['source_record'] == values['sense_record'] and model['capability'] == '' and model['messages_json'] == ''
    assert plan == dict(run_id=ROBOT_RUN, capability='', prompt_name='', world_model=values['world_model'],
                        source_record=values['model_record'])
    status, body = decide(world, started['turn_id'], True)
    assert status == 200
    record = turn_state(world['base'], started['turn_id'], 'completed')
    act = calls(world)['act'][-1]
    assert act['action_json'] == values['action_json'] and act['source_record'] == values['plan_record']
    assert record['values']['act_success'] is True and record['values']['act_record'].startswith('state/cycle_000001/act')
    run = robot_run(world)
    for key in ('sense_record', 'model_record', 'plan_record', 'act_record', 'snapshot_path'):
        assert (run / record['values'][key]).is_file(), key
    assert record['snapshot']['path'] == record['values']['snapshot_path']
    assert record['message'] == 'Look at the card' and record['values']['belief_context_id'] == ROBOT_RUN


def test_next_step_carries_the_latest_snapshot_like_smpas_experiment(world):
    """A later step passes the snapshot the latest Model produced as previous_snapshot."""
    context(world, 'shared')
    latest = get(world['base'], f'/smpa/sessions/{ROBOT_RUN}/world-model')['snapshots'][-1]['path']
    started = step(world)
    turn_state(world['base'], started['turn_id'], 'awaiting_act')
    assert calls(world)['model'][-1]['previous_snapshot'] == latest
    decide(world, started['turn_id'], False)
    record = turn_state(world['base'], started['turn_id'], 'declined')
    assert record['stages']['act']['state'] == 'declined'


def test_declining_act_calls_nothing_and_a_second_decision_is_refused(world):
    """Declining ends the step without Act; deciding again, or for a turn not waiting, is refused (409)."""
    context(world, 'shared')
    before = len(calls(world)['act'])
    started = step(world)
    turn_state(world['base'], started['turn_id'], 'awaiting_act')
    assert decide(world, started['turn_id'], False)[0] == 200
    turn_state(world['base'], started['turn_id'], 'declined')
    assert len(calls(world)['act']) == before
    assert decide(world, started['turn_id'], True)[0] == 409


def test_failed_stage_reports_smpas_recorded_reason_and_retry_resumes(world):
    """An aborted Plan is explained with the reason its record holds; the retry calls only Plan and Act."""
    context(world, 'shared')
    (world['memory'] / 'scenario.json').write_text(json.dumps(dict(fail='plan')))
    before = calls(world)
    failed = turn_state(world['base'], step(world)['turn_id'], 'failed')
    assert failed['failed_stage'] == 'plan' and failed['stages']['plan']['state'] == 'aborted'
    assert 'recorded reason: RuntimeError: Scripted plan failure' in failed['error']
    assert failed['snapshot']['path']  # Model had committed its snapshot before Plan failed
    status, body = post(world['base'], f"/smpa/dialogue/turns/{failed['turn_id']}/retry", dict(request_id=rid('retry')),
                        world['token'])
    assert status == 202, body
    retried = turn_state(world['base'], body['turn']['turn_id'], 'awaiting_act')
    decide(world, retried['turn_id'], True)
    retried = turn_state(world['base'], retried['turn_id'], 'completed')
    after = calls(world)
    assert [len(after[stage]) - len(before[stage]) for stage in ('sense', 'model', 'plan', 'act')] == [1, 1, 2, 1]
    assert retried['retry_of'] == failed['turn_id'] and retried['stages']['sense']['state'] == 'skipped'
    assert retried['values']['model_record'] == failed['values']['model_record']


def test_act_reporting_failure_is_shown_and_the_next_step_works(world):
    """Act answering success false fails the step; the failed Act record is on disk; the next step completes."""
    context(world, 'shared')
    (world['memory'] / 'scenario.json').write_text(json.dumps(dict(fail='act')))
    started = step(world)
    turn_state(world['base'], started['turn_id'], 'awaiting_act')
    decide(world, started['turn_id'], True)
    failed = turn_state(world['base'], started['turn_id'], 'failed')
    assert failed['failed_stage'] == 'act' and 'did not succeed' in failed['error']
    acts = get(world['base'], f'/smpa/sessions/{ROBOT_RUN}/records', stage='act')['records']
    assert acts[-1]['status'] == 'failed' and acts[-1]['summary']['success'] is False
    again = step(world)
    turn_state(world['base'], again['turn_id'], 'awaiting_act')
    decide(world, again['turn_id'], True)
    assert turn_state(world['base'], again['turn_id'], 'completed')['values']['act_success'] is True


def test_requests_follow_the_change_rules_and_do_not_block(world):
    """A step needs the token; one runs at a time; other routes answer at once while a step waits for Act."""
    status, _ = post(world['base'], '/smpa/dialogue/turns', dict(request_id='notoken-0001'), 'wrong')
    assert status == 403
    context(world, 'shared')
    started = step(world)
    turn_state(world['base'], started['turn_id'], 'awaiting_act')
    busy = post(world['base'], '/smpa/dialogue/turns', dict(request_id=rid('busy')), world['token'])
    assert busy[0] == 409 and 'still running' in busy[1]['detail']
    moment = time.monotonic()
    for _ in range(5):
        get(world['base'], '/health')
        get(world['base'], '/smpa/dialogue')
    assert time.monotonic() - moment < 2.5
    decide(world, started['turn_id'], False)
    turn_state(world['base'], started['turn_id'], 'declined')


# ----- persisted history ------------------------------------------------------------------------------

def test_records_and_snapshots_are_read_from_the_run_with_their_stated_links(world):
    """Each stage's records come from SMPA's files with the links the records state; evidence is served read-only."""
    sense = get(world['base'], f'/smpa/sessions/{ROBOT_RUN}/records', stage='sense')['records']
    assert sense and all(record['stage'] == 'sense' for record in sense)
    evidence = [link for link in sense[0]['links'] if link['relation'] == 'evidence']
    assert evidence[0]['kind'] == 'image' and evidence[0]['exists']
    with urlopen(world['base'] + API + f'/smpa/sessions/{ROBOT_RUN}/files?' + urlencode(dict(path=evidence[0]['path']))) as image:
        assert image.read().startswith(b'\x89PNG')
    model = get(world['base'], f'/smpa/sessions/{ROBOT_RUN}/records', stage='model')['records'][0]
    assert {link['relation'] for link in model['links']} >= {'source', 'evidence', 'produced_snapshot'}
    assert model['summary']['iteration'] == model['record']['iteration']
    plan = get(world['base'], f'/smpa/sessions/{ROBOT_RUN}/records', stage='plan')['records'][0]
    assert plan['summary']['selected_action'] == 'observe' and plan['summary']['parameters']['question']
    assert plan['summary']['available_actions'] == ['observe', 'turn_left']
    snapshots = get(world['base'], f'/smpa/sessions/{ROBOT_RUN}/world-model')['snapshots']
    assert snapshots[0]['produced_by'] == model['path'] and 'Stand-in world model' in snapshots[0]['text']
    everything = get(world['base'], f'/smpa/sessions/{ROBOT_RUN}/records')
    sequences = [record['sequence'] for record in everything['records']]
    assert sequences == sorted(sequences) and everything['revision']


# ----- lifecycle and inference management -------------------------------------------------------------

def test_robot_run_start_and_stop_go_through_smpas_run_control(world):
    """Stop and start call SMPA's StopSmpa and StartSmpa once each; a repeated request id changes nothing."""
    stop_id = rid('stop')
    status, body = post(world['base'], '/smpa/ros/run/stop', dict(request_id=stop_id), world['token'])
    assert status == 200 and body['run'] == dict(accepted=True)
    assert post(world['base'], '/smpa/ros/run/stop', dict(request_id=stop_id), world['token'])[1]['run']['duplicate'] is True
    assert get(world['base'], '/smpa/ros')['robot']['run_state'] == 'completed'
    status, body = post(world['base'], '/smpa/ros/run/start', dict(goal='Find the card', request_id=rid('start')),
                        world['token'])
    assert status == 200 and body['run'] == dict(accepted=True, run_id=ROBOT_RUN)
    counted = calls(world)
    assert len(counted['stop']) == 1 and counted['start'][-1] == dict(goal='Find the card')
    assert post(world['base'], '/smpa/ros/run/start', dict(goal='x', request_id=rid('notoken')), 'wrong')[0] == 403


def test_model_load_is_followed_in_the_background(world):
    """Loading a model answers 202 at once; the status route reports loading, then the outcome from LoadModel."""
    status, body = post(world['base'], '/smpa/ros/llm/load', dict(model_id='stub-b', request_id=rid('load')), world['token'])
    assert status == 202 and body['model_load']['state'] == 'loading'
    wait_until(lambda: get(world['base'], '/smpa/ros')['model_load']['state'] != 'loading', 15)
    answer = get(world['base'], '/smpa/ros')
    assert answer['model_load']['state'] == 'loaded' and answer['inference']['status']['active_model'] == 'stub-b'
    post(world['base'], '/smpa/ros/llm/load', dict(model_id='unknown', request_id=rid('load')), world['token'])
    wait_until(lambda: get(world['base'], '/smpa/ros')['model_load']['state'] != 'loading', 15)
    failed = get(world['base'], '/smpa/ros')['model_load']
    assert failed['state'] == 'failed' and 'Unknown registered model' in failed['error']
    post(world['base'], '/smpa/ros/llm/load', dict(model_id='stub-a', request_id=rid('load')), world['token'])
    wait_until(lambda: get(world['base'], '/smpa/ros')['model_load']['state'] == 'loaded', 15)


# ----- the stream -------------------------------------------------------------------------------------

def test_stream_carries_step_events_live_stage_status_and_smpa_topics(world):
    """The stream sends a status, the step's events in order, the Act request, live stage states and SMPA's topics."""
    import websockets
    context(world, 'shared')
    url = world['base'].replace('http', 'ws') + API + '/smpa/dialogue/stream'

    async def scenario():
        """Open the stream, run a step (approving Act when asked), and collect messages until it completed."""
        received = []
        async with websockets.connect(url, origin=world['base']) as socket:
            first = json.loads(await socket.recv())
            assert first['kind'] == 'status' and first['state'] == 'ok' and first['relays_unavailable'] == []
            identity = (await asyncio.to_thread(step, world))['turn_id']
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(socket.recv(), 10))
                received.append(message)
                if message.get('event') == 'act_confirmation_required' and message.get('turn_id') == identity:
                    await asyncio.to_thread(decide, world, identity, True)
                events = {item.get('event') for item in received}
                if any(item.get('event') == 'turn_completed' and item.get('turn_id') == identity for item in received) \
                        and {'smpa_event', 'inference_status', 'stage_status'} <= events:
                    break
        return identity, received

    identity, received = asyncio.run(scenario())
    mine = [(item['event'], item.get('stage')) for item in received if item.get('turn_id') == identity]
    assert mine[0] == ('turn_started', None) and mine[-1] == ('turn_completed', None)
    assert [stage for event, stage in mine if event == 'stage_started'] == ['sense', 'model', 'plan', 'act']
    events = [event for event, _ in mine]
    assert mine[events.index('model_snapshot') - 1] == ('stage_completed', 'model')  # read only after Model committed
    assert events.index('act_confirmation_required') < events.index('act_decision') < len(events) - 3
    live = [item for item in received if item.get('event') == 'stage_status']
    assert {item['message']['stage'] for item in live} <= {'sense', 'model', 'plan', 'act'} and live
    smpa = next(item for item in received if item.get('event') == 'smpa_event')
    assert smpa['message'] == dict(run_id=ROBOT_RUN, stage='model', event='stage_started')


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
        """Tell whether the GUI node no longer subscribes to SMPA's event or stage status topics."""
        graph = get(world['base'], '/ros/graph')
        return not any(edge['kind'] == 'subscribes' and edge['target'] == 'node:/smpa_gui'
                       and edge['source'].startswith('topic:/smpa/') for edge in graph['edges'])
    wait_until(released, 10, 'the relay subscriptions were not released')


def test_gui_stops_cleanly_while_a_step_waits_for_act(world, tmp_path):
    """Stopping the GUI during a step leaves Act uncalled and the process exits cleanly."""
    process, base = start_server(world['environment'])
    try:
        token = get(base, '/session')['token']
        post(base, '/smpa/dialogue/context', dict(mode='shared'), token)
        wait_until(lambda: get(base, '/smpa/ros')['robot']['state'] == 'ok')
        before = len(calls(world)['act'])
        status, body = post(base, '/smpa/dialogue/turns', dict(request_id=rid('shutdown')), token)
        turn_state(base, body['turn']['turn_id'], 'awaiting_act')
    finally:
        stop(process)
    assert process.returncode == 0 and len(calls(world)['act']) == before


def test_read_only_and_disabled_guis_refuse_changes(tmp_path):
    """A read-only GUI refuses steps, decisions and lifecycle changes; a GUI without SMPA answers 404."""
    memory = write_memory(tmp_path / 'memory')
    for flag, expected in (('SMPA_GUI_READ_ONLY', 403), ('SMPA_GUI_NO_SMPA', 404)):
        process, base = start_server(dict(isolated_environment(), SMPA_GUI_MEMORY_ROOT=str(memory), **{flag: '1'}))
        try:
            token = get(base, '/session')['token'] or 'none'
            for path, body in (('/smpa/dialogue/turns', dict(request_id='refused-0001')),
                               ('/smpa/dialogue/context', dict(mode='shared')),
                               ('/smpa/dialogue/turns/x/act', dict(approve=True, request_id='refused-0002')),
                               ('/smpa/ros/run/start', dict(goal='go', request_id='refused-0003')),
                               ('/smpa/ros/run/stop', dict(request_id='refused-0004')),
                               ('/smpa/ros/llm/load', dict(model_id='a', request_id='refused-0005'))):
                assert post(base, path, body, token)[0] == expected, path
            if expected == 404:
                with pytest.raises(HTTPError):
                    get(base, '/smpa/ros')
        finally:
            stop(process)
