"""Integration tests of parameter management and service management against real ROS 2 nodes.

Nothing here is mocked: a real GUI server process talks to the real fixture
nodes in ``fixtures/management_nodes.py`` over DDS, and results are compared
with the standard ``ros2 param`` and ``ros2 service`` tools where that helps.
"""
import json
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from conftest import GUI, PYTHON, fetch, start_server, stop

API = '/api/v1'
SETTINGS = '/fixture_mgmt/settings'


def wait_until(condition, timeout=30.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.3)
    raise AssertionError(message)


def get(base, path, **parameters):
    """Fetch one JSON endpoint with encoded query parameters."""
    return fetch(base + API + path + ('?' + urlencode(parameters) if parameters else ''), timeout=15)


def post(base, path, body, token, timeout=90):
    """POST JSON with the session token; return (status, body) without raising on HTTP errors."""
    request = Request(base + API + path, data=json.dumps(body).encode(), method='POST',
                      headers={'Content-Type': 'application/json', 'X-SMPA-GUI-Token': token})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def start_fixture(environment, *arguments):
    """Start the management fixture process and wait until its nodes exist."""
    process = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/management_nodes.py'), *arguments], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = process.stdout.readline()
    if 'MANAGEMENT READY' not in line:
        process.kill()
        raise RuntimeError('Management fixtures failed: ' + line + process.stdout.read())
    return process


@pytest.fixture(scope='module')
def world(ros_environment, server):
    """Run the management fixtures beside the shared GUI server; yield the server, environment and token."""
    process = start_fixture(ros_environment)
    wait_until(lambda: any(item['name'] == '/fixture/add_two_ints' for item in get(server, '/ros/services')['services']),
               message='GUI never discovered the fixture services')
    wait_until(lambda: get(server, '/ros/nodes/parameters', node=SETTINGS)['state'] == 'ok',
               message='the settings node never answered parameter requests')
    token = get(server, '/session')['token']
    yield dict(base=server, environment=ros_environment, token=token, process=process)
    stop(process)


def parameters(world):
    """Read the settings node's parameters through the GUI, as a dict by name."""
    result = get(world['base'], '/ros/nodes/parameters', node=SETTINGS)
    assert result['state'] == 'ok', result
    return {item['name']: item for item in result['parameters']}


def ros2(world, *arguments):
    """Run a ros2 command-line tool in the test domain and return its standard output."""
    return subprocess.run(['ros2', *arguments], env=world['environment'], capture_output=True, text=True,
                          timeout=60).stdout


def change(world, changes, request_id, atomic=True, node=SETTINGS):
    """Send one parameter change request and return (status, body)."""
    return post(world['base'], '/ros/parameters/set',
                dict(node=node, changes=changes, atomic=atomic, request_id=request_id), world['token'])


def counts(world):
    """Ask the fixture how often each service was called (this query itself is not counted)."""
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture/call_counts', type='std_srvs/srv/Trigger', request={},
                             request_id=f'counts{time.monotonic_ns()}'), world['token'])
    assert status == 200, body
    return json.loads(body['call']['response']['message'])


# ----- parameters ----------------------------------------------------------------------------------

def test_parameters_are_read_with_descriptors(world):
    """Values, types, descriptions, read-only flags, dynamic typing, constraints and ranges are reported."""
    found = parameters(world)
    assert found['level']['value'] == 4 and found['level']['type'] == 'integer'
    assert found['level']['integer_range'] == {'from': 0, 'to': 10, 'step': 2}
    assert found['speed']['floating_point_range'] == {'from': 0.0, 'to': 2.0, 'step': 0.0}
    assert found['frozen']['read_only'] is True and found['label']['read_only'] is False
    assert found['anything']['dynamic_typing'] is True and found['label']['dynamic_typing'] is False
    assert found['guarded']['additional_constraints'] == 'Any text except "forbidden"'
    assert found['label']['description'] == 'A free text label' and found['label']['descriptor_known'] is True
    assert found['gains']['type'] == 'double_array' and found['names']['value'] == ['a', 'b']


NONFINITE = '/fixture_mgmt/nonfinite'


def strict(text):
    """Parse a response body as standard JSON, refusing the NaN and Infinity tokens Python would otherwise accept."""
    def refuse(token):
        """Fail on a token that standard JSON does not have."""
        raise AssertionError(f'non-standard JSON token {token}')
    return json.loads(text, parse_constant=refuse)


def strict_get(base, path, **parameters):
    """Fetch one endpoint, require HTTP 200, and parse its body as standard JSON."""
    with urlopen(base + API + path + ('?' + urlencode(parameters) if parameters else ''), timeout=15) as response:
        assert response.status == 200
        return strict(response.read())


def test_non_finite_parameters_are_served_by_name(world):
    """NaN and infinite doubles, array elements and range bounds of a real node are sent as "nan", "inf", "-inf".

    Before the fix these routes failed with "Out of range float values are
    not JSON compliant". Finite values, integers and types stay unchanged.
    """
    reading = strict_get(world['base'], '/ros/nodes/parameters', node=NONFINITE)
    found = {item['name']: item for item in reading['parameters']}
    assert reading['state'] == 'ok'
    assert (found['nan_value']['value'], found['nan_value']['type']) == ('nan', 'double')
    assert found['positive_infinity']['value'] == 'inf' and found['negative_infinity']['value'] == '-inf'
    assert found['mixed_array']['value'] == [1.5, 'nan', 'inf', '-inf'] and found['mixed_array']['type'] == 'double_array'
    assert found['unbounded']['value'] == 0.5
    assert found['unbounded']['floating_point_range'] == {'from': '-inf', 'to': 'inf', 'step': 0.0}
    assert found['finite']['value'] == 0.25 and found['finite']['type'] == 'double'
    wait_until(lambda: any(item['node'] == NONFINITE for item in strict_get(world['base'], '/ros/parameters')['nodes']),
               message='the overview never listed the non-finite node')
    nodes = {item['node']: item for item in strict_get(world['base'], '/ros/parameters')['nodes']}
    assert {item['name']: item['value'] for item in nodes[NONFINITE]['parameters']}['nan_value'] == 'nan'
    assert nodes[SETTINGS]['state'] == 'ok'  # one node's NaN no longer breaks the whole overview
    assert {item['name']: item['value'] for item in nodes[SETTINGS]['parameters']}['level'] in (4, 6, 8)


def test_non_finite_names_and_values_are_never_set(world):
    """A displayed name ("nan") or a raw NaN/Infinity sent as a value is refused, and nothing changes on the node."""
    for request_id, value in (('nonfinite-0001', 'nan'), ('nonfinite-0002', float('nan')), ('nonfinite-0003', float('inf'))):
        status, body = change(world, [dict(name='finite', type='double', value=value)], request_id, node=NONFINITE)
        assert status == 422 and body['errors'] == [dict(path='finite', message='must be a finite number')], body
    status, body = change(world, [dict(name='mixed_array', type='double_array', value=[1.5, 'nan', 'inf', '-inf'])],
                          'nonfinite-0004', node=NONFINITE)
    assert status == 422 and [error['path'] for error in body['errors']] == ['mixed_array[1]', 'mixed_array[2]', 'mixed_array[3]']
    status, body = post(world['base'], '/ros/services/call', dict(service='/fixture/add_two_ints', type='example_interfaces/srv/AddTwoInts',
                        request=dict(a=1, b=2), request_id='nonfinite-0005', timeout=float('nan')), world['token'])
    assert status == 422 and body['detail'][0]['input'] == 'nan'  # FastAPI's own validation answer, made JSON-safe
    found = {item['name']: item['value'] for item in get(world['base'], '/ros/nodes/parameters', node=NONFINITE)['parameters']}
    assert found['finite'] == 0.25 and found['mixed_array'] == [1.5, 'nan', 'inf', '-inf']


def test_finite_value_replaces_nan_and_the_record_names_the_old_value(world):
    """Setting a NaN parameter to a finite number works; the change record reports the old value as "nan"."""
    status, body = change(world, [dict(name='nan_value', type='double', value=2.5)], 'nonfinite-0006', node=NONFINITE)
    assert status == 200, body
    result = body['change']['results'][0]
    assert body['change']['outcome'] == 'applied' and result['previous_value'] == 'nan' and result['value'] == 2.5
    assert strict_get(world['base'], '/ros/parameters/changes')['changes'][0]['change_id'] == 'nonfinite-0006'
    found = {item['name']: item['value'] for item in get(world['base'], '/ros/nodes/parameters', node=NONFINITE)['parameters']}
    assert found['nan_value'] == 2.5
    wait_until(lambda: 'Double value is: 2.5' in ros2(world, 'param', 'get', NONFINITE, 'nan_value'),
               message='ros2 param get never reported the new value')  # the CLI's discovery can lag


def test_successful_change_is_applied_once_and_read_back(world):
    """A valid atomic change is applied, read back, visible to ros2 param, and a repeated request changes nothing."""
    status, body = change(world, [dict(name='label', type='string', value='beta'),
                                  dict(name='level', type='integer', value=8)], 'param-ok-0001')
    assert status == 200, body
    record = body['change']
    assert record['outcome'] == 'applied' and record['atomic']
    assert [(entry['name'], entry['previous_value'], entry['value'], entry['successful']) for entry in record['results']] \
        == [('label', 'alpha', 'beta', True), ('level', 4, 8, True)]
    read_back = {item['name']: item['value'] for item in record['parameters']}
    assert read_back['label'] == 'beta' and read_back['level'] == 8
    wait_until(lambda: 'beta' in ros2(world, 'param', 'get', SETTINGS, 'label'), message='ros2 param get disagrees')
    status, again = change(world, [dict(name='label', type='string', value='gamma')], 'param-ok-0001')
    assert status == 200 and again['change'].get('duplicate') and parameters(world)['label']['value'] == 'beta'
    history = get(world['base'], '/ros/parameters/changes')['changes']
    assert sum(1 for item in history if item['change_id'] == 'param-ok-0001') == 1


def test_arrays_and_dynamic_typing(world):
    """Array parameters take lists of their element type; dynamic typing allows a type change."""
    status, body = change(world, [dict(name='gains', type='double_array', value=[0.5, 1, 2.25]),
                                  dict(name='names', type='string_array', value=['x']),
                                  dict(name='counts', type='integer_array', value=[]),
                                  dict(name='anything', type='string', value='now text')], 'param-arr-0001')
    assert status == 200 and body['change']['outcome'] == 'applied', body
    found = parameters(world)
    assert found['gains']['value'] == [0.5, 1.0, 2.25] and found['names']['value'] == ['x']
    assert found['counts']['value'] == [] and found['anything']['type'] == 'string'


@pytest.mark.parametrize('proposed, fragment', [
    (dict(name='frozen', type='integer', value=8), 'read-only'),
    (dict(name='level', type='integer', value=5), 'steps of 2'),
    (dict(name='level', type='integer', value=12), 'within 0…10'),
    (dict(name='speed', type='double', value=2.5), 'within 0.0…2.0'),
    (dict(name='label', type='integer', value=3), 'must stay of type string'),
    (dict(name='enabled', type='bool', value=1), 'true or false'),
    (dict(name='level', type='integer', value=2.5), 'whole number'),
    (dict(name='gains', type='double_array', value=[1, 'x']), 'finite number'),
    (dict(name='no_such_parameter', type='integer', value=1), 'is not a parameter'),
])
def test_invalid_changes_are_refused_before_anything_is_sent(world, proposed, fragment):
    """Read-only, range, step, type and value problems are reported per parameter and nothing changes."""
    before = parameters(world)
    companion = 'guarded' if proposed['name'] == 'label' else 'label'
    status, body = change(world, [dict(name=companion, type='string', value='should-not-apply'), proposed],
                          f'param-bad-{abs(hash(fragment)) % 10 ** 8:08d}')
    assert status == 422, body
    assert any(fragment in error['message'] for error in body['errors']), body['errors']
    after = parameters(world)
    assert {name: item['value'] for name, item in after.items()} == {name: item['value'] for name, item in before.items()}


def test_node_rejection_atomic_and_individual(world):
    """The node's own rejection is shown with its reason; atomic requests change nothing, individual ones partly."""
    status, body = change(world, [dict(name='label', type='string', value='delta'),
                                  dict(name='guarded', type='string', value='forbidden')], 'param-rej-0001')
    assert status == 200 and body['change']['outcome'] == 'rejected'
    assert all(entry['reason'] == 'forbidden is not allowed here' for entry in body['change']['results'])
    assert parameters(world)['label']['value'] != 'delta'
    status, body = change(world, [dict(name='label', type='string', value='delta'),
                                  dict(name='guarded', type='string', value='forbidden')], 'param-rej-0002', atomic=False)
    assert status == 200 and body['change']['outcome'] == 'partial'
    results = {entry['name']: entry for entry in body['change']['results']}
    assert results['label']['successful'] is True and results['guarded']['successful'] is False
    assert results['guarded']['reason'] == 'forbidden is not allowed here'
    found = parameters(world)
    assert found['label']['value'] == 'delta' and found['guarded']['value'] == 'ok'


def test_missing_and_unresponsive_nodes_are_refused(world):
    """A node that does not exist or does not answer cannot be changed; nothing is sent."""
    status, body = change(world, [dict(name='x', type='integer', value=1)], 'param-miss-0001', node='/no_such_node')
    assert status == 409 and 'unavailable' in body['detail']
    status, body = change(world, [dict(name='never_readable', type='integer', value=2)], 'param-miss-0002',
                          node='/fixture_silent')
    assert status == 409 and 'not responding' in body['detail']


def test_duplicate_parameter_names_and_read_only_gui(world):
    """A request naming one parameter twice is refused; without the token nothing is accepted."""
    status, _ = change(world, [dict(name='label', type='string', value='a'), dict(name='label', type='string', value='b')],
                       'param-dup-0001')
    assert status == 422
    status, _ = post(world['base'], '/ros/parameters/set',
                     dict(node=SETTINGS, changes=[dict(name='label', type='string', value='z')], request_id='param-tok-0001'), '')
    assert status == 403 and parameters(world)['label']['value'] != 'z'


# ----- services ------------------------------------------------------------------------------------

def test_services_are_discovered_with_kinds_and_providers(world):
    """Services are listed with types, kinds, providers and clients, matching ros2 service list."""
    services = {item['name']: item for item in get(world['base'], '/ros/services')['services']}
    add = services['/fixture/add_two_ints']
    assert add['types'] == ['example_interfaces/srv/AddTwoInts'] and add['kind'] == 'other'
    assert add['providers'] == ['/fixture_mgmt/services']
    assert services[SETTINGS + '/set_parameters']['kind'] == 'parameter'
    assert any(item['kind'] == 'action' for item in services.values())
    listed = set()

    def listed_by_ros2():
        """Ask ros2 service list until it reports the fixture services (discovery is asynchronous)."""
        listed.clear()
        listed.update(ros2(world, 'service', 'list', '--no-daemon').split())
        return {'/fixture/add_two_ints', '/fixture/nested', '/fixture/slow'} <= listed

    wait_until(listed_by_ros2, message='ros2 service list never showed the fixture services')
    assert {name for name in services if name.startswith('/fixture/')} >= {name for name in listed if name.startswith('/fixture/')}


def test_service_interface_describes_nested_messages_and_arrays(world):
    """The request schema of SetParameters has an array of nested messages; defaults come from the definition."""
    described = get(world['base'], '/ros/services/interface', type='rcl_interfaces/srv/SetParameters')
    parameters_field = described['request']['fields'][0]
    assert parameters_field['name'] == 'parameters' and parameters_field['schema']['kind'] == 'array'
    item = parameters_field['schema']['item']
    assert item['type'] == 'rcl_interfaces/msg/Parameter'
    value = next(field for field in item['fields'] if field['name'] == 'value')['schema']
    assert any(field['name'] == 'integer_array_value' and field['schema']['kind'] == 'array' for field in value['fields'])
    assert described['request_default'] == {'parameters': []}
    assert described['response']['fields'][0]['name'] == 'results'
    with pytest.raises(HTTPError) as unknown:
        get(world['base'], '/ros/services/interface', type='no_pkg/srv/Nothing')
    assert unknown.value.code == 404


def test_successful_call_happens_once(world):
    """A confirmed call is made exactly once; repeating its request id returns the first record without calling."""
    before = counts(world).get('add_two_ints', 0)
    request = dict(service='/fixture/add_two_ints', type='example_interfaces/srv/AddTwoInts', request={'a': 2, 'b': 40},
                   request_id='svc-add-0001')
    status, body = post(world['base'], '/ros/services/call', request, world['token'])
    assert status == 200 and body['call']['state'] == 'succeeded' and body['call']['response'] == {'sum': 42}
    status, again = post(world['base'], '/ros/services/call', request, world['token'])
    assert status == 200 and again['call'].get('duplicate') and again['call']['call_id'] == body['call']['call_id']
    assert counts(world).get('add_two_ints', 0) == before + 1
    history = get(world['base'], '/ros/services/calls')['calls']
    assert sum(1 for item in history if item['request_id'] == 'svc-add-0001') == 1
    assert get(world['base'], f"/ros/services/calls/{body['call']['call_id']}")['call']['response'] == {'sum': 42}


def test_nested_request_with_arrays(world):
    """A request with an array of nested messages holding arrays arrives intact."""
    request = {'parameters': [{'name': 'p1', 'value': {'type': 7, 'integer_array_value': [1, 2, 3]}},
                              {'name': 'p2', 'value': {'type': 4, 'string_value': 'x'}}]}
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture/nested', type='rcl_interfaces/srv/SetParameters', request=request,
                             request_id='svc-nest-0001'), world['token'])
    assert status == 200, body
    assert [item['reason'] for item in body['call']['response']['results']] == ['echo:p1:7:3', 'echo:p2:4:0']


@pytest.mark.parametrize('request_value, fragment', [
    ({'a': 'two', 'b': 1}, 'whole number'),
    ({'a': 2 ** 63, 'b': 1}, 'between'),
    ({'a': 1, 'c': 1}, 'is not a field'),
])
def test_invalid_requests_are_refused_without_calling(world, request_value, fragment):
    """Values that do not fit the request type give per-field errors and the service is not called."""
    before = counts(world).get('add_two_ints', 0)
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture/add_two_ints', type='example_interfaces/srv/AddTwoInts', request=request_value,
                             request_id=f'svc-bad-{abs(hash(fragment)) % 10 ** 8:08d}'), world['token'])
    assert status == 422 and any(fragment in error['message'] for error in body['errors']), body
    assert counts(world).get('add_two_ints', 0) == before


def test_timeout_is_bounded_reported_and_not_retried(world):
    """A slow service that misses the timeout is reported as timed out after about the timeout, and called once."""
    before = counts(world).get('slow', 0)
    started = time.monotonic()
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture/slow', type='std_srvs/srv/Trigger', request={}, request_id='svc-slow-0001',
                             timeout=1.0), world['token'])
    elapsed = time.monotonic() - started
    assert status == 504 and body['call']['state'] == 'timed_out' and 'not repeated' in body['call']['error']
    assert 0.9 < elapsed < 3.0
    time.sleep(2.5)
    assert counts(world).get('slow', 0) == before + 1
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture/slow', type='std_srvs/srv/Trigger', request={}, request_id='svc-slow-0002',
                             timeout=6.0), world['token'])
    assert status == 200 and body['call']['response'] == {'success': True, 'message': 'slow answer'}


def test_unavailable_service_is_refused_without_blocking(world):
    """A service without a provider is refused within a few seconds; other requests are served meanwhile."""
    started = time.monotonic()
    status, body = post(world['base'], '/ros/services/call',
                        dict(service='/fixture/nobody_here', type='std_srvs/srv/Trigger', request={},
                             request_id='svc-none-0001'), world['token'])
    assert status == 409 and 'nothing was sent' in body['detail'] and time.monotonic() - started < 5
    assert get(world['base'], '/health')['status'] == 'ok'


def test_provider_disappearing_during_a_call(world):
    """When the provider leaves during a call, the call ends as server_lost well before its timeout.

    The provider is stopped with Ctrl+C, so it leaves the ROS graph properly.
    A provider that is killed outright is noticed only when DDS discovery
    times out (typically 10 to 20 seconds); then the call still ends as
    server_lost or timed_out, never by blocking.
    """
    vanishing = start_fixture(world['environment'], '--vanishing')
    try:
        wait_until(lambda: any(item['name'] == '/fixture/vanish' for item in get(world['base'], '/ros/services')['services']))
        import threading
        outcome = {}

        def call():
            """Make the call that will lose its provider."""
            outcome['answer'] = post(world['base'], '/ros/services/call',
                                     dict(service='/fixture/vanish', type='std_srvs/srv/Trigger', request={},
                                          request_id='svc-vanish-0001', timeout=30.0), world['token'])

        caller = threading.Thread(target=call)
        started = time.monotonic()
        caller.start()
        time.sleep(1.5)
        assert get(world['base'], '/health')['status'] == 'ok'
        import signal
        vanishing.send_signal(signal.SIGINT)
        caller.join(30)
        status, body = outcome['answer']
        assert status == 504 and body['call']['state'] == 'server_lost' and time.monotonic() - started < 15
    finally:
        if vanishing.poll() is None:
            vanishing.kill()


def test_gui_holds_no_service_clients_when_idle(world):
    """After calls finish, the GUI node holds no service clients."""
    wait_until(lambda: not any('/smpa_gui' in item['clients'] for item in get(world['base'], '/ros/services')['services']),
               timeout=10, message='the GUI still holds service clients')


def test_read_only_gui_refuses_changes_and_calls(ros_environment, world):
    """In read-only mode parameter changes and service calls are refused; reading still works."""
    process, base = start_server(dict(ros_environment, SMPA_GUI_READ_ONLY='1'))
    try:
        assert get(base, '/session')['enabled'] is False
        status, body = post(base, '/ros/parameters/set', dict(node=SETTINGS, changes=[dict(name='label', type='string',
                            value='ro')], request_id='param-ro-0001'), 'anything')
        assert status == 403 and 'read-only' in body['detail']
        status, _ = post(base, '/ros/services/call', dict(service='/fixture/add_two_ints', type='example_interfaces/srv/AddTwoInts',
                         request={'a': 1, 'b': 1}, request_id='svc-ro-0001'), 'anything')
        assert status == 403
        wait_until(lambda: get(base, '/ros/nodes/parameters', node=SETTINGS)['state'] == 'ok')
        assert parameters(world)['label']['value'] != 'ro'
    finally:
        stop(process)
