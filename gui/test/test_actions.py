"""Tests of interactive action execution against real ROS 2 fixture action servers.

The server under test is the real GUI process; goals travel over HTTP exactly
as the browser sends them. The Fibonacci fixture server sends feedback five
times per second and has special orders: negative is rejected, 13 aborts, 7
refuses cancellation.
"""
import json
import os
import subprocess
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from conftest import GUI, PYTHON, fetch, start_server, stop

API = '/api/v1'
FIBONACCI = dict(action='/fixture/fibonacci', type='example_interfaces/action/Fibonacci')
LOOKUP = dict(action='/fixture/lookup_transform', type='tf2_msgs/action/LookupTransform')


def token(base):
    """Fetch the session token the way the page does."""
    return fetch(base + API + '/actions/session')['token']


def post(base, path, body, headers=None, session=True):
    """Send a JSON POST and return the HTTP status and parsed body, also for error statuses."""
    send = {'Content-Type': 'application/json'}
    if session:
        send['X-SMPA-GUI-Token'] = token(base)
    send.update(headers or {})
    request = Request(base + API + path, data=json.dumps(body).encode(), headers=send, method='POST')
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def submit(base, target, goal, request_id=None):
    """Submit a goal and return the status and the response body."""
    return post(base, '/actions/goals', dict(target, goal=goal, request_id=request_id or uuid.uuid4().hex))


def goal(base, goal_id):
    """Read one goal record."""
    return fetch(f'{base}{API}/actions/goals/{goal_id}')['goal']


def wait_for_state(base, goal_id, states, timeout=20.0):
    """Wait until a goal reaches one of the given states and return its record."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = goal(base, goal_id)
        if record['state'] in states:
            return record
        time.sleep(0.1)
    raise AssertionError(f"goal stayed {record['state']}, expected one of {states}")


def wait_until(condition, timeout=15.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(message)


def gateway_is_action_client(base):
    """Ask ROS discovery whether the gateway node currently appears as a client of the Fibonacci action."""
    actions = fetch(base + API + '/ros/actions')['actions']
    return '/smpa_gui' in next(item for item in actions if item['name'] == '/fixture/fibonacci')['clients']


def test_servers_are_discovered_with_availability(server):
    """Both fixture actions are listed with their types and marked available."""
    actions = {item['name']: item for item in fetch(server + API + '/actions/servers')['actions']}
    assert actions['/fixture/fibonacci']['types'] == ['example_interfaces/action/Fibonacci']
    assert actions['/fixture/fibonacci']['available'] and actions['/fixture/fibonacci']['servers'] == ['/fixture_action_server']
    assert actions['/fixture/lookup_transform']['types'] == ['tf2_msgs/action/LookupTransform']


def test_interface_is_derived_from_the_ros_definition(server):
    """Goal, feedback and result fields come from the installed interface, including nested messages."""
    simple = fetch(server + API + '/actions/interface?' + urlencode(dict(type=FIBONACCI['type'])))
    assert simple['goal']['fields'] == [dict(name='order', schema=dict(kind='integer', type='int32',
                                                                     minimum=-2 ** 31, maximum=2 ** 31 - 1))]
    assert simple['goal_default'] == {'order': 0}
    assert simple['feedback']['fields'][0]['schema']['kind'] == 'array'
    nested = fetch(server + API + '/actions/interface?' + urlencode(dict(type=LOOKUP['type'])))
    fields = {field['name']: field['schema'] for field in nested['goal']['fields']}
    assert fields['target_frame'] == dict(kind='string', type='string', max_length=None)
    assert fields['advanced'] == dict(kind='boolean', type='boolean')
    assert fields['source_time']['type'] == 'builtin_interfaces/msg/Time'
    assert [field['name'] for field in fields['source_time']['fields']] == ['sec', 'nanosec']
    assert nested['goal_default']['timeout'] == {'sec': 0, 'nanosec': 0}
    with pytest.raises(HTTPError) as missing:
        fetch(server + API + '/actions/interface?' + urlencode(dict(type='no_pkg/action/Nothing')))
    assert missing.value.code == 404


def test_goal_succeeds_with_feedback_result_uuid_and_timestamps(server):
    """A submitted goal is accepted, streams feedback, and ends with its result and timestamps."""
    before = time.time()
    status, body = submit(server, FIBONACCI, {'order': 5})
    assert status == 201
    record = body['goal']
    assert uuid.UUID(record['goal_id']) and record['goal'] == {'order': 5} and record['state'] in ('sending', 'accepted', 'executing')
    running = wait_for_state(server, record['goal_id'], ('executing',))
    assert running['accepted_at'] >= before and running['finished_at'] is None
    done = wait_for_state(server, record['goal_id'], ('succeeded',))
    assert done['result'] == {'sequence': [0, 1, 1, 2, 3, 5]}
    assert done['feedback_count'] == 4 and [item['number'] for item in done['feedback']] == [1, 2, 3, 4]
    assert done['feedback'][-1]['message'] == {'sequence': [0, 1, 1, 2, 3, 5]}
    assert before <= done['created_at'] <= done['accepted_at'] <= done['finished_at']
    assert done['error'] is None and done['cancel_requested_at'] is None
    assert fetch(server + API + '/actions/goals')['goals'][0]['goal_id'] == record['goal_id']


def test_nested_goal_values_reach_the_server_intact(server):
    """Strings, booleans and nested time values arrive at the action server unchanged."""
    values = {'target_frame': 'map', 'source_frame': 'base_link', 'source_time': {'sec': 12, 'nanosec': 34},
              'timeout': {'sec': 3, 'nanosec': 0}, 'advanced': True}
    status, body = submit(server, LOOKUP, values)
    assert status == 201
    done = wait_for_state(server, body['goal']['goal_id'], ('succeeded',))
    transform = done['result']['transform']
    assert transform['header']['frame_id'] == 'map' and transform['child_frame_id'] == 'base_link'
    assert transform['header']['stamp'] == {'sec': 12, 'nanosec': 34}
    assert transform['transform']['translation']['x'] == 3.0 and transform['transform']['translation']['y'] == 1.0
    assert done['goal']['fixed_frame'] == '' and done['goal']['target_time'] == {'sec': 0, 'nanosec': 0}


@pytest.mark.parametrize('target, values, path, fragment', [
    (FIBONACCI, {'order': 'five'}, 'order', 'whole number'),
    (FIBONACCI, {'order': 2.5}, 'order', 'whole number'),
    (FIBONACCI, {'order': True}, 'order', 'whole number'),
    (FIBONACCI, {'order': 2 ** 31}, 'order', 'between'),
    (FIBONACCI, {'order': 1, 'speed': 2}, 'speed', 'not a field'),
    (LOOKUP, {'advanced': 1}, 'advanced', 'true or false'),
    (LOOKUP, {'target_frame': 7}, 'target_frame', 'text'),
    (LOOKUP, {'source_time': {'sec': -2 ** 40}}, 'source_time.sec', 'between'),
    (LOOKUP, {'source_time': 5}, 'source_time', 'object'),
])
def test_invalid_goal_fields_are_refused_and_nothing_is_sent(server, target, values, path, fragment):
    """Values that do not fit the ROS field types give 422 naming the field, and create no goal."""
    count = len(fetch(server + API + '/actions/goals')['goals'])
    status, body = submit(server, target, values)
    assert status == 422
    assert [error['path'] for error in body['errors']] == [path] and fragment in body['errors'][0]['message']
    assert len(fetch(server + API + '/actions/goals')['goals']) == count


def test_rejected_and_aborted_goals_are_reported(server):
    """A goal the server rejects ends as rejected; a goal the server aborts ends as aborted with its result."""
    status, body = submit(server, FIBONACCI, {'order': -1})
    assert status == 201
    rejected = wait_for_state(server, body['goal']['goal_id'], ('rejected',))
    assert rejected['accepted_at'] is None and 'rejected' in rejected['error'] and rejected['result'] is None
    _, body = submit(server, FIBONACCI, {'order': 13})
    aborted = wait_for_state(server, body['goal']['goal_id'], ('aborted',))
    assert aborted['result'] == {'sequence': [0, 1, 1]} and aborted['feedback_count'] == 1


def test_cancellation(server):
    """An active goal can be cancelled; finished goals cannot; a server may refuse cancellation."""
    _, body = submit(server, FIBONACCI, {'order': 40})
    goal_id = body['goal']['goal_id']
    wait_for_state(server, goal_id, ('executing',))
    status, cancel = post(server, f'/actions/goals/{goal_id}/cancel', {})
    assert status == 200 and cancel['goal']['cancel_requested_at'] is not None
    canceled = wait_for_state(server, goal_id, ('canceled',))
    assert canceled['cancel_answer'] == 'accepted' and 2 <= len(canceled['result']['sequence']) < 40
    status, again = post(server, f'/actions/goals/{goal_id}/cancel', {})
    assert status == 409 and 'canceled' in again['detail']
    assert post(server, '/actions/goals/' + str(uuid.uuid4()) + '/cancel', {})[0] == 404

    _, body = submit(server, FIBONACCI, {'order': 7})
    stubborn = body['goal']['goal_id']
    wait_for_state(server, stubborn, ('executing',))
    post(server, f'/actions/goals/{stubborn}/cancel', {})
    wait_until(lambda: goal(server, stubborn)['cancel_answer'] == 'rejected', message='the refusal was not recorded')
    assert wait_for_state(server, stubborn, ('succeeded',))['result']['sequence'][-1] == 13


def test_missing_server_and_unknown_type(server):
    """Without an action server the goal is refused with 409; an uninstalled type gives 404."""
    started = time.monotonic()
    status, body = submit(server, dict(FIBONACCI, action='/nobody/serves/this'), {'order': 1})
    assert status == 409 and 'No action server' in body['detail'] and time.monotonic() - started < 6
    status, body = submit(server, dict(action='/fixture/fibonacci', type='no_pkg/action/Nothing'), {})
    assert status == 404 and 'not available' in body['detail']
    wait_until(lambda: not gateway_is_action_client(server))


def test_concurrent_goals_run_independently(server):
    """Several goals on the same server run at the same time and keep their own feedback and results."""
    ids = {order: submit(server, FIBONACCI, {'order': order})[1]['goal']['goal_id'] for order in (4, 6, 8)}
    assert len(set(ids.values())) == 3
    wait_until(lambda: all(goal(server, goal_id)['state'] == 'executing' for goal_id in ids.values()),
               message='the goals did not execute concurrently')
    for order, goal_id in ids.items():
        done = wait_for_state(server, goal_id, ('succeeded',))
        assert len(done['result']['sequence']) == order + 1 and done['feedback_count'] == order - 1


def test_repeating_a_request_identifier_never_sends_a_second_goal(server):
    """A retried submission with the same request identifier returns the first goal."""
    request_id = uuid.uuid4().hex
    count = len(fetch(server + API + '/actions/goals')['goals'])
    status, first = submit(server, FIBONACCI, {'order': 3}, request_id)
    status_again, second = submit(server, FIBONACCI, {'order': 3}, request_id)
    assert (status, status_again) == (201, 200) and second['goal']['duplicate'] is True
    assert second['goal']['goal_id'] == first['goal']['goal_id']
    wait_for_state(server, first['goal']['goal_id'], ('succeeded',))
    assert submit(server, FIBONACCI, {'order': 9}, request_id)[1]['goal']['goal'] == {'order': 3}
    assert len(fetch(server + API + '/actions/goals')['goals']) == count + 1


def test_reading_never_sends_goals(server):
    """Reading servers, interfaces, the session and the history any number of times creates no goal."""
    count = len(fetch(server + API + '/actions/goals')['goals'])
    for _ in range(3):
        fetch(server + API + '/actions/servers')
        fetch(server + API + '/actions/session')
        fetch(server + API + '/actions/interface?' + urlencode(dict(type=FIBONACCI['type'])))
        fetch(server + API + '/actions/goals')
    assert len(fetch(server + API + '/actions/goals')['goals']) == count


def test_action_client_exists_only_while_a_goal_is_active(server):
    """The gateway appears as an action client during a goal and disappears from the ROS graph afterwards."""
    wait_until(lambda: not gateway_is_action_client(server))
    _, body = submit(server, FIBONACCI, {'order': 12})
    wait_until(lambda: gateway_is_action_client(server), message='the gateway never appeared as an action client')
    post(server, f"/actions/goals/{body['goal']['goal_id']}/cancel", {})
    wait_for_state(server, body['goal']['goal_id'], ('canceled',))
    wait_until(lambda: not gateway_is_action_client(server), message='the action client was not removed after the goal')


def test_access_controls(server):
    """Goals need the session token, a JSON body, a local origin and a local Host header."""
    body = dict(FIBONACCI, goal={'order': 1}, request_id=uuid.uuid4().hex)
    count = len(fetch(server + API + '/actions/goals')['goals'])
    assert post(server, '/actions/goals', body, session=False)[0] == 403
    assert post(server, '/actions/goals', body, headers={'X-SMPA-GUI-Token': 'wrong'}, session=False)[0] == 403
    assert post(server, '/actions/goals', body, headers={'Origin': 'https://example.com'})[0] == 403
    assert post(server, '/actions/goals', body, headers={'Host': 'attacker.example:8765'})[0] == 403
    assert post(server, '/actions/goals', body, headers={'Content-Type': 'text/plain'})[0] in (415, 422)
    assert post(server, '/actions/goals', dict(body, request_id='x'))[0] == 422
    assert post(server, '/actions/goals', dict(body, action='no-leading-slash'))[0] == 422
    request = Request(server + API + '/actions/session', headers={'Origin': 'https://example.com'})
    with pytest.raises(HTTPError) as foreign:
        urlopen(request, timeout=5)
    assert foreign.value.code == 403
    assert len(fetch(server + API + '/actions/goals')['goals']) == count
    assert post(server, '/actions/goals', body, headers={'Origin': server})[0] == 201


def run_lonely(arguments=()):
    """Start fixture nodes and a GUI server with extra arguments in a fresh isolated domain."""
    from conftest import isolated_environment
    environment = isolated_environment()
    fixtures = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/fixture_nodes.py')], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert 'FIXTURES READY' in fixtures.stdout.readline()
    import conftest
    original = conftest.PYTHON
    port = conftest.free_port()
    process = subprocess.Popen([original, '-m', 'smpa_gui', '--port', str(port), *arguments], cwd=GUI,
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f'http://127.0.0.1:{port}'
    wait_until(lambda: reachable(base), timeout=20, message='the GUI server did not start')
    wait_until(lambda: any(item['available'] for item in fetch(base + API + '/actions/servers')['actions']), timeout=30,
               message='the action server was not discovered')
    return environment, fixtures, process, base


def reachable(base):
    """Tell whether the GUI server answers its health check."""
    try:
        fetch(base + API + '/health', timeout=1)
        return True
    except OSError:
        return False


def fibonacci_status(environment):
    """Read the Fibonacci server's own goal status list with the ros2 command-line tool.

    The status topic lists every goal the server knows with its status
    number (2 executing, 4 succeeded, 5 cancelled). Return those numbers.
    """
    output = subprocess.run(['ros2', 'topic', 'echo', '--once', '--qos-durability', 'transient_local',
                             '--qos-reliability', 'reliable', '/fixture/fibonacci/_action/status'],
                            env=environment, capture_output=True, text=True, timeout=30).stdout
    return [int(line.split(':')[1]) for line in output.splitlines() if line.strip().startswith('status:')]


def test_read_only_mode_refuses_goals():
    """With --read-only the session offers no token and goals are refused."""
    environment, fixtures, process, base = run_lonely(['--read-only'])
    try:
        session = fetch(base + API + '/actions/session')
        assert session['enabled'] is False and session['token'] is None
        body = dict(FIBONACCI, goal={'order': 1}, request_id=uuid.uuid4().hex)
        status, answer = post(base, '/actions/goals', body, headers={'X-SMPA-GUI-Token': 'anything'}, session=False)
        assert status == 403 and 'read-only' in answer['detail']
        assert fetch(base + API + '/actions/goals')['goals'] == []
    finally:
        stop(process)
        stop(fixtures)


def test_stopping_the_gui_cancels_active_goals_by_default():
    """Ctrl+C on the GUI asks the server to cancel the goals the GUI still has running."""
    environment, fixtures, process, base = run_lonely()
    try:
        _, body = submit(base, FIBONACCI, {'order': 200})
        wait_for_state(base, body['goal']['goal_id'], ('executing',))
        stop(process)
        assert process.returncode == 0
        wait_until(lambda: fibonacci_status(environment) == [5], timeout=40,
                   message='the action server did not report the goal as cancelled')
    finally:
        if process.poll() is None:
            stop(process)
        stop(fixtures)


def test_goals_survive_the_gui_when_asked_to_and_new_gateways_start_empty():
    """With --keep-goals-on-exit the goal keeps executing on its server; a restarted GUI has no history."""
    environment, fixtures, process, base = run_lonely(['--keep-goals-on-exit'])
    try:
        old_token = token(base)
        _, body = submit(base, FIBONACCI, {'order': 200})
        wait_for_state(base, body['goal']['goal_id'], ('executing',))
        stop(process)
        wait_until(lambda: fibonacci_status(environment) == [2], timeout=30,
                   message='the goal must still be executing on the server')
        process, base = start_server(environment)
        assert fetch(base + API + '/actions/goals')['goals'] == []
        stale = dict(FIBONACCI, goal={'order': 1}, request_id=uuid.uuid4().hex)
        assert post(base, '/actions/goals', stale, headers={'X-SMPA-GUI-Token': old_token}, session=False)[0] == 403
    finally:
        if process.poll() is None:
            stop(process)
        stop(fixtures)


def test_a_vanished_server_marks_the_goal_as_lost():
    """If the action server process dies during a goal, the goal ends as lost instead of hanging forever."""
    environment, fixtures, process, base = run_lonely()
    try:
        _, body = submit(base, FIBONACCI, {'order': 200})
        wait_for_state(base, body['goal']['goal_id'], ('executing',))
        fixtures.kill()
        fixtures.wait()
        lost = wait_for_state(base, body['goal']['goal_id'], ('lost',), timeout=40)
        assert 'disappeared' in lost['error'] and lost['result'] is None and lost['finished_at'] is not None
        assert fetch(base + API + '/health')['status'] == 'ok'
    finally:
        stop(process)
        if fixtures.poll() is None:
            stop(fixtures)
