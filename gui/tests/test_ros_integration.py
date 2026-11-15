"""Tests of the running GUI server against real ROS 2 fixture nodes.

Each test asks the server over HTTP or WebSocket, exactly like the browser
does, and compares the answer with what the fixtures are known to provide or
with what the standard ``ros2`` command-line tools report in the same domain.
"""
import io
import json
import shutil
import struct
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

import pytest
from websockets.sync.client import connect

from conftest import fetch, stop

API = '/api/v1'


def get(base, path, **parameters):
    """Fetch one snapshot endpoint with encoded query parameters."""
    return fetch(base + API + path + ('?' + urlencode(parameters) if parameters else ''))


def stream(base, path, **parameters):
    """Open a stream endpoint as a WebSocket client."""
    return connect(base.replace('http', 'ws') + API + path + '?' + urlencode(parameters), max_size=None)


def next_data(socket, timeout=10.0):
    """Read messages until a data message arrives; return its header and binary block.

    Status messages before the first data are returned as well, so a test
    can check them. A data header that announces a binary block is followed
    by exactly one binary message.
    """
    statuses = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(socket.recv(timeout=timeout))
        if message['kind'] == 'status':
            statuses.append(message)
            continue
        binary = socket.recv(timeout=timeout) if message.get('binary') else None
        return message, binary, statuses
    raise AssertionError(f'No data message; statuses: {statuses}')


def cli(environment, *arguments):
    """Run a ros2 command in the test domain and return its output lines."""
    result = subprocess.run(['ros2', *arguments], env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def wait_until(condition, timeout=10.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(message)


def assert_agreement(ours, theirs, timeout=40.0):
    """Assert that the GUI and a command-line tool report the same thing.

    Both sides discover the ROS graph asynchronously, and a freshly started
    command-line tool may answer before its own discovery is complete. So
    both are asked repeatedly until they agree; if they still differ after
    the timeout, the last pair is compared so the difference is shown.
    """
    deadline = time.monotonic() + timeout
    while True:
        mine, other = ours(), theirs()
        if mine == other or time.monotonic() > deadline:
            assert mine == other
            return
        time.sleep(0.5)


needs_cli = pytest.mark.skipif(shutil.which('ros2') is None, reason='the ros2 command-line tool is not installed')


def test_health_reports_ros_environment(server, ros_environment):
    """The server reports the domain it joined and starts with no subscriptions."""
    health = get(server, '/health')
    assert health['status'] == 'ok' and health['ros']['ok'] and health['ros']['node'] == '/smpa_gui'
    assert health['ros']['domain_id'] == int(ros_environment['ROS_DOMAIN_ID'])
    assert health['contract_version'] == '1.2'


def test_graph_contains_fixture_nodes_topics_and_connections(server):
    """The graph shows the talker publishing and the listener subscribing to the same topic."""
    graph = get(server, '/ros/graph')
    names = {node['full_name'] for node in graph['nodes']}
    assert {'/fixture_talker', '/fixture_listener', '/fixture_ns/fixture_parameters', '/fixture_silent',
            '/fixture_sensors', '/fixture_action_server', '/fixture_action_client', '/smpa_gui'} <= names
    assert next(node for node in graph['nodes'] if node['full_name'] == '/smpa_gui')['is_gateway']
    edges = {(edge['source'], edge['target'], edge['kind']) for edge in graph['edges']}
    assert ('node:/fixture_talker', 'topic:/fixture/chatter', 'publishes') in edges
    assert ('topic:/fixture/chatter', 'node:/fixture_listener', 'subscribes') in edges
    status = next(topic for topic in graph['topics'] if topic['name'] == '/fixture/fibonacci/_action/status')
    assert status['hidden'] and status['action'] == '/fixture/fibonacci'


@needs_cli
def test_nodes_match_ros2_node_list(server, ros_environment):
    """The node list equals `ros2 node list`, apart from the command-line tool's own node."""
    assert_agreement(
        lambda: {node['full_name'] for node in get(server, '/ros/nodes')['nodes'] if not node['hidden']},
        lambda: {name for name in cli(ros_environment, 'node', 'list', '--no-daemon', '--spin-time', '3')
                 if name.startswith('/')})


@needs_cli
def test_topics_match_ros2_topic_list(server, ros_environment):
    """Topic names and types equal `ros2 topic list -t`, including hidden topics."""
    def theirs():
        """Parse `name [type, ...]` lines printed by the command-line tool."""
        found = set()
        for line in cli(ros_environment, 'topic', 'list', '-t', '--no-daemon', '--include-hidden-topics',
                        '--spin-time', '3'):
            name, types = line.split(' ', 1)
            found.add((name, tuple(part.strip() for part in types.strip('[]').split(','))))
        return found

    assert_agreement(
        lambda: {(topic['name'], tuple(topic['types'])) for topic in get(server, '/ros/topics')['topics']}, theirs)


@needs_cli
def test_actions_match_ros2_action_list(server, ros_environment):
    """Actions, their types, servers and clients equal what `ros2 action` reports."""
    assert_agreement(
        lambda: [(action['name'], action['types']) for action in get(server, '/ros/actions')['actions']],
        lambda: [(name, [types.strip('[]')]) for name, types in
                 (line.split(' ', 1) for line in cli(ros_environment, 'action', 'list', '-t'))])
    actions = get(server, '/ros/actions')['actions']
    assert actions[0]['servers'] == ['/fixture_action_server'] and actions[0]['clients'] == ['/fixture_action_client']
    assert_agreement(
        lambda: ['Action: /fixture/fibonacci', 'Action clients: 1', '/fixture_action_client',
                 'Action servers: 1', '/fixture_action_server'],
        lambda: cli(ros_environment, 'action', 'info', '/fixture/fibonacci'))


def test_topic_info_lists_endpoints_with_qos(server):
    """Topic details name the publisher and subscriber nodes and their quality of service."""
    topic = get(server, '/ros/topics/info', name='/fixture/chatter')['topic']
    assert topic['types'] == ['std_msgs/msg/String']
    assert [item['node'] for item in topic['publishers']] == ['/fixture_talker']
    assert [item['node'] for item in topic['subscribers']] == ['/fixture_listener']
    assert topic['publishers'][0]['qos']['reliability'] == 'reliable'
    assert topic['publishers'][0]['qos']['durability'] == 'volatile'
    image = get(server, '/ros/topics/info', name='/fixture/camera/image_raw')['topic']
    assert image['publishers'][0]['qos']['reliability'] == 'best_effort'
    with pytest.raises(HTTPError) as missing:
        get(server, '/ros/topics/info', name='/does/not/exist')
    assert missing.value.code == 404


def test_echo_streams_live_messages_and_latched_messages(server):
    """The generic echo delivers changing live messages and a message published before it subscribed."""
    with stream(server, '/ros/topics/echo', name='/fixture/chatter') as socket:
        first, _, _ = next_data(socket)
        second, _, _ = next_data(socket)
    assert first['message']['data'].startswith('fixture message') and first['message'] != second['message']
    assert first['type'] == 'std_msgs/msg/String' and first['truncated'] is False
    with stream(server, '/ros/topics/echo', name='/fixture/latched') as socket:
        assert next_data(socket)[0]['message'] == {'data': 'published once'}


def test_echo_shortens_large_messages(server):
    """Echoing an image topic sends a bounded summary instead of every pixel."""
    with stream(server, '/ros/topics/echo', name='/fixture/camera/image_raw') as socket:
        message, _, _ = next_data(socket)
    assert message['truncated'] and message['message']['encoding'] == 'rgb8'
    assert message['message']['data']['length'] == 64 * 48 * 3 and len(message['message']['data']['head']) == 64


def test_parameters_are_read_with_types_values_and_descriptions(server):
    """Declared parameters are listed with their types and current values."""
    result = get(server, '/ros/nodes/parameters', node='/fixture_ns/fixture_parameters')
    assert result['state'] == 'ok'
    found = {item['name']: item for item in result['parameters']}
    assert (found['fixture_integer']['type'], found['fixture_integer']['value']) == ('integer', 42)
    assert (found['fixture_string']['type'], found['fixture_string']['value']) == ('string', 'hello')
    assert (found['fixture_double']['type'], found['fixture_double']['value']) == ('double', 1.5)
    assert (found['fixture_flags']['type'], found['fixture_flags']['value']) == ('bool_array', [True, False])
    assert found['use_sim_time']['value'] is False and 'description' in found['fixture_integer']


@needs_cli
def test_parameters_match_ros2_param(server, ros_environment):
    """Parameter names and one value equal what `ros2 param` reports for the same node."""
    node = '/fixture_ns/fixture_parameters'
    assert_agreement(
        lambda: {item['name'] for item in get(server, '/ros/nodes/parameters', node=node)['parameters']},
        lambda: set(cli(ros_environment, 'param', 'list', node, '--no-daemon', '--spin-time', '3')))
    assert_agreement(
        lambda: [f"Integer value is: {next(item['value'] for item in get(server, '/ros/nodes/parameters', node=node)['parameters'] if item['name'] == 'fixture_integer')}"],
        lambda: cli(ros_environment, 'param', 'get', node, 'fixture_integer', '--no-daemon', '--spin-time', '3'))


def test_unresponsive_and_unknown_nodes_are_states_not_errors(server):
    """A node that never spins is 'not_responding'; a node without services is 'unavailable'."""
    started = time.monotonic()
    silent = get(server, '/ros/nodes/parameters', node='/fixture_silent', timeout=1)
    assert silent == dict(contract_version='1.2', node='/fixture_silent', state='not_responding', parameters=[])
    assert time.monotonic() - started < 4
    assert get(server, '/ros/nodes/parameters', node='/no_such_node')['state'] == 'unavailable'
    assert get(server, '/ros/nodes/parameters', node='/smpa_gui')['state'] == 'unavailable'
    assert get(server, '/health')['status'] == 'ok'


def test_sensor_discovery_lists_every_fixture_sensor(server):
    """Every fixture sensor topic is discovered with the right viewer; other topics are not."""
    topics = {item['topic']: item for item in get(server, '/sensors/topics')['topics']}
    expected = {'/fixture/camera/image_raw': 'image', '/fixture/camera/depth': 'image',
                '/fixture/camera/image_raw/compressed': 'image', '/fixture/camera/camera_info': 'camerainfo',
                '/fixture/points': 'pointcloud', '/fixture/scan': 'laserscan', '/fixture/imu': 'imu',
                '/fixture/joint_states': 'jointstate', '/fixture/gps/fix': 'navsatfix', '/fixture/range': 'range'}
    for topic, viewer in expected.items():
        assert topics[topic]['viewer'] == viewer and topics[topic]['publisher_count'] == 1
    assert '/fixture/chatter' not in topics
    assert len(get(server, '/sensors/registry')['sensors']) == 9


def test_image_streams_deliver_correct_pictures(server):
    """Raw, depth and compressed image streams deliver JPEG frames with the fixture's red corner."""
    from PIL import Image
    with stream(server, '/sensors/stream', topic='/fixture/camera/image_raw') as socket:
        header, jpeg, _ = next_data(socket)
        header_two, jpeg_two, _ = next_data(socket)
    picture = Image.open(io.BytesIO(jpeg)).convert('RGB')
    red, green, blue = picture.getpixel((2, 2))
    assert picture.size == (64, 48) and red > 200 and green < 70 and blue < 70
    assert header['encoding'] == 'rgb8' and header['frame_id'] == 'fixture_camera' and jpeg != jpeg_two
    with stream(server, '/sensors/stream', topic='/fixture/camera/depth') as socket:
        header, jpeg, _ = next_data(socket)
    assert header['scaled'] and Image.open(io.BytesIO(jpeg)).size == (16, 8)
    with stream(server, '/sensors/stream', topic='/fixture/camera/image_raw/compressed') as socket:
        header, jpeg, _ = next_data(socket)
    assert header['format'].startswith('rgb8; jpeg') and Image.open(io.BytesIO(jpeg)).size == (64, 48)


def test_unsupported_image_encoding_is_reported_once_and_stream_survives(server):
    """An undrawable encoding yields an 'unsupported' status, not a broken connection."""
    with stream(server, '/sensors/stream', topic='/fixture/camera/unsupported') as socket:
        status = json.loads(socket.recv(timeout=10))
        assert status['kind'] == 'status' and status['state'] == 'unsupported' and 'bayer_rggb16' in status['detail']
        with pytest.raises(TimeoutError):
            socket.recv(timeout=1.5)


def test_point_cloud_stream(server):
    """The point cloud stream delivers valid, coloured, optionally thinned points."""
    with stream(server, '/sensors/stream', topic='/fixture/points') as socket:
        header, block, _ = next_data(socket)
    assert header['binary'] == 'points' and header['stride'] == 6 and header['colour'] == 'rgb'
    assert header['total_points'] == 5000 and header['valid_points'] == 4999 and header['count'] == 4999
    values = struct.unpack(f'<{header["count"] * 6}f', block)
    for index in range(0, len(values), 6 * 500):
        x, y, z, r, g, b = values[index:index + 6]
        assert abs((x * x + y * y + z * z) ** 0.5 - 1.0) < 1e-4 and 0 <= r <= 1 and 0 <= g <= 1 and 0 <= b <= 1
    with stream(server, '/sensors/stream', topic='/fixture/points', max_points=500) as socket:
        header, block, _ = next_data(socket)
    assert header['count'] <= 500 and len(block) == header['count'] * 24


def test_scalar_sensor_streams(server):
    """Scan, IMU, joint, position, range and calibration streams carry the fixture's values."""
    def one(topic):
        """Read one data message from a sensor topic."""
        with stream(server, '/sensors/stream', topic=topic) as socket:
            return next_data(socket)[0]

    scan = one('/fixture/scan')
    assert scan['total_rays'] == 360 and scan['ranges'][0] is None and scan['ranges'][90] == pytest.approx(2.0, abs=1e-3)
    imu = one('/fixture/imu')
    assert imu['has_orientation'] and imu['orientation']['w'] == pytest.approx(0.7071, abs=1e-3)
    assert imu['angular_velocity']['z'] == 0.5 and imu['linear_acceleration']['z'] == pytest.approx(9.81)
    joints = one('/fixture/joint_states')
    assert joints['names'] == ['fixture_joint_a', 'fixture_joint_b', 'fixture_joint_c'] and joints['effort'] == []
    fix = one('/fixture/gps/fix')
    assert fix['valid'] and (fix['latitude'], fix['longitude']) == (52.334, 4.866) and fix['horizontal_accuracy_m'] == 2.0
    reading = one('/fixture/range')
    assert reading['range'] == 1.0 and reading['radiation_type'] == 'ultrasound'
    info = one('/fixture/camera/camera_info')
    assert (info['width'], info['height']) == (64, 48) and info['k'][0] == 50.0 and len(info['p']) == 12


def test_stream_rate_is_limited(server):
    """A ten-per-second topic requested at two per second arrives at about two per second."""
    with stream(server, '/sensors/stream', topic='/fixture/imu', rate=2) as socket:
        next_data(socket)
        started = time.monotonic()
        for _ in range(4):
            next_data(socket)
        elapsed = time.monotonic() - started
    assert 1.5 < elapsed < 3.5


def test_subscription_exists_only_while_a_stream_is_open(server, ros_environment):
    """Opening a stream subscribes, two viewers share it, and closing the last one unsubscribes."""
    def gateway_subscribes():
        """Ask ROS discovery (not the server's own bookkeeping) whether the gateway subscribes."""
        topic = get(server, '/ros/topics/info', name='/fixture/scan')['topic']
        return '/smpa_gui' in [item['node'] for item in topic['subscribers']]

    wait_until(lambda: get(server, '/health')['active_subscriptions'] == [] and not gateway_subscribes(),
               message='an earlier stream left a subscription behind')
    first = stream(server, '/sensors/stream', topic='/fixture/scan')
    second = stream(server, '/sensors/stream', topic='/fixture/scan')
    next_data(first), next_data(second)
    active = get(server, '/health')['active_subscriptions']
    assert active == [dict(topic='/fixture/scan', type='sensor_msgs/msg/LaserScan', viewers=2)]
    wait_until(gateway_subscribes, message='the subscription never became visible in the ROS graph')
    if shutil.which('ros2'):
        assert_agreement(lambda: True, lambda: 'Subscription count: 1' in cli(
            ros_environment, 'topic', 'info', '/fixture/scan', '--no-daemon', '--spin-time', '3'))
    first.close()
    wait_until(lambda: get(server, '/health')['active_subscriptions'][0]['viewers'] == 1)
    second.close()
    wait_until(lambda: get(server, '/health')['active_subscriptions'] == [])
    wait_until(lambda: not gateway_subscribes(), message='the gateway stayed subscribed after its viewers left')
    if shutil.which('ros2'):
        assert_agreement(lambda: True, lambda: 'Subscription count: 0' in cli(
            ros_environment, 'topic', 'info', '/fixture/scan', '--no-daemon', '--spin-time', '3'))


def test_topic_switching_moves_the_subscription(server):
    """Closing one sensor stream and opening another leaves only the new subscription."""
    scan = stream(server, '/sensors/stream', topic='/fixture/scan')
    next_data(scan)
    scan.close()
    with stream(server, '/sensors/stream', topic='/fixture/range') as reading:
        next_data(reading)
        wait_until(lambda: [item['topic'] for item in get(server, '/health')['active_subscriptions']] == ['/fixture/range'])
    wait_until(lambda: get(server, '/health')['active_subscriptions'] == [])


def test_streams_explain_silence_and_refuse_wrong_types(server):
    """A topic without publishers reports 'no_publishers'; a non-sensor type is refused with advice."""
    with stream(server, '/sensors/stream', topic='/nobody/publishes/scan', type='sensor_msgs/msg/LaserScan') as socket:
        status = json.loads(socket.recv(timeout=10))
        assert status['state'] == 'no_publishers'
    with stream(server, '/sensors/stream', topic='/unknown/topic') as socket:
        assert json.loads(socket.recv(timeout=10))['state'] == 'error'
    with stream(server, '/sensors/stream', topic='/fixture/chatter') as socket:
        refusal = json.loads(socket.recv(timeout=10))
        assert refusal['state'] == 'error' and 'Topics view' in refusal['detail']
    with stream(server, '/ros/topics/echo', name='/x', type='no_such_pkg/msg/Nothing') as socket:
        assert 'not available' in json.loads(socket.recv(timeout=10))['detail']
    wait_until(lambda: get(server, '/health')['active_subscriptions'] == [])


def test_single_frame_and_motion_jpeg_endpoints(server):
    """The plain HTTP image endpoints work for non-browser clients and clean up after themselves."""
    address = server + API + '/sensors/image/frame.jpg?topic=' + quote('/fixture/camera/image_raw', safe='')
    with urlopen(address, timeout=10) as response:
        assert response.headers['Content-Type'] == 'image/jpeg' and response.read()[:2] == b'\xff\xd8'
    with pytest.raises(HTTPError) as unsupported:
        urlopen(server + API + '/sensors/image/frame.jpg?topic=' + quote('/fixture/camera/unsupported', safe=''), timeout=10)
    assert unsupported.value.code == 415
    with pytest.raises(HTTPError) as not_image:
        urlopen(server + API + '/sensors/image/frame.jpg?topic=' + quote('/fixture/scan', safe=''), timeout=10)
    assert not_image.value.code == 404
    motion = urlopen(server + API + '/sensors/image/stream.mjpg?topic=' + quote('/fixture/camera/image_raw', safe=''), timeout=10)
    assert motion.headers['Content-Type'].startswith('multipart/x-mixed-replace')
    chunk = motion.read(400)
    assert b'--frame' in chunk and b'Content-Type: image/jpeg' in chunk
    motion.close()
    wait_until(lambda: get(server, '/health')['active_subscriptions'] == [], timeout=15,
               message='the motion-JPEG subscription was not released after the client left')


def test_foreign_browser_origins_cannot_open_streams(server):
    """A page loaded from another site is refused; a local page is accepted."""
    from websockets.exceptions import InvalidStatus
    address = server.replace('http', 'ws') + API + '/ros/topics/echo?name=' + quote('/fixture/chatter', safe='')
    with pytest.raises(InvalidStatus):
        connect(address, origin='https://example.com')
    with connect(address, origin=server) as socket:
        assert next_data(socket)[0]['message']['data'].startswith('fixture message')


def test_only_the_action_goal_routes_can_change_anything(server):
    """Every route is read-only except submitting a goal and requesting its cancellation."""
    routes = fetch(server + '/api/openapi.json')['paths']
    writable = {path: sorted(methods) for path, methods in routes.items() if set(methods) != {'get'}}
    assert writable == {'/api/v1/actions/goals': ['get', 'post'],
                        '/api/v1/actions/goals/{goal_id}/cancel': ['post']}


def test_gateway_node_offers_nothing_to_the_ros_graph(server):
    """While idle the gateway has no subscriptions, no services, and only the unavoidable publisher."""
    wait_until(lambda: get(server, '/health')['active_subscriptions'] == [])
    time.sleep(1.5)
    graph = get(server, '/ros/graph')
    own = [(edge['kind'], edge['source'], edge['target']) for edge in graph['edges'] if 'node:/smpa_gui' in (edge['source'], edge['target'])]
    assert own == [('publishes', 'node:/smpa_gui', 'topic:/parameter_events')]
    assert [service['name'] for service in graph['services'] if service['name'].startswith('/smpa_gui/')] == []


def test_runs_alone_and_discovers_nodes_that_start_later(lonely_server):
    """With nothing else in the domain the GUI works; nodes that appear and vanish are followed."""
    base, environment, _ = lonely_server
    graph = get(base, '/ros/graph')
    assert [node['full_name'] for node in graph['nodes']] == ['/smpa_gui'] and graph['actions'] == []
    assert get(base, '/sensors/topics')['topics'] == []
    assert get(base, '/ros/actions')['actions'] == []
    from conftest import GUI, PYTHON
    late = subprocess.Popen([PYTHON, str(GUI / 'tests/fixtures/fixture_nodes.py')], env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert 'FIXTURES READY' in late.stdout.readline()
        wait_until(lambda: any(item['topic'] == '/fixture/scan' for item in get(base, '/sensors/topics')['topics']),
                   timeout=30, message='late fixture nodes were not discovered')
        socket = stream(base, '/sensors/stream', topic='/fixture/scan')
        next_data(socket)
    finally:
        stop(late)
    statuses = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(item.get('state') == 'no_publishers' for item in statuses):
        try:
            message = json.loads(socket.recv(timeout=2))
        except TimeoutError:
            continue
        if message['kind'] == 'status':
            statuses.append(message)
    socket.close()
    assert any(item['state'] == 'no_publishers' for item in statuses), statuses
    wait_until(lambda: '/fixture_talker' not in {node['full_name'] for node in get(base, '/ros/graph')['nodes']}, timeout=30)
    assert get(base, '/health')['status'] == 'ok'


def test_stopping_the_gui_leaves_other_nodes_running(lonely_server):
    """Interrupting the GUI ends it cleanly and removes only its own node from the graph."""
    base, environment, process = lonely_server
    from conftest import GUI, PYTHON
    other = subprocess.Popen([PYTHON, str(GUI / 'tests/fixtures/fixture_nodes.py')], env=environment,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert 'FIXTURES READY' in other.stdout.readline()
        wait_until(lambda: len(get(base, '/ros/nodes')['nodes']) > 1, timeout=30)
        stop(process)
        assert process.returncode == 0
        assert other.poll() is None, 'fixture nodes must survive the GUI stopping'
        if shutil.which('ros2'):
            def others_without_gui():
                """Check that the fixtures are listed and the GUI node is gone."""
                names = cli(environment, 'node', 'list', '--no-daemon', '--spin-time', '3')
                return '/fixture_talker' in names and '/smpa_gui' not in names

            wait_until(others_without_gui, timeout=40, message='ros2 node list still shows the GUI or lost the fixtures')
    finally:
        stop(other)
