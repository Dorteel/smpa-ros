"""Tests of the SMPA session routes of a real GUI server, with a real stand-in for an active session."""
import json
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from conftest import GUI, PYTHON, fetch, smpa_memory, start_server, stop
from test_smpa_runs import tree_fingerprint

API = '/api/v1/smpa'
ROBOT = '11111111-1111-4111-8111-111111111111'
ACTIVE = '55555555-5555-4555-8555-555555555555'


def wait_until(condition, timeout=30.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.3)
    raise AssertionError(message)


@pytest.fixture(scope='module')
def active_run(ros_environment, server):
    """Run the stand-in for an active SMPA robot session in the test domain."""
    process = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/active_run_node.py'), ACTIVE], env=ros_environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert 'ACTIVE RUN READY' in process.stdout.readline()
    yield process
    stop(process)


def session(server, identity):
    """Fetch one session description."""
    return fetch(server + f'{API}/sessions/{identity}')['session']


def test_status_and_sessions(server):
    """The configured fixture memory is reported and every fixture session is listed."""
    status = fetch(server + API + '/status')
    assert status['configured'] is True and status['memory_root'] == smpa_memory() and status['demonstration'] is False
    sessions = fetch(server + API + '/sessions')['sessions']
    assert len(sessions) == 9 and sessions[0]['session_id'] == ACTIVE
    assert {item['activity'] for item in sessions} >= {'ended', 'running_not_visible', 'running_unverified', 'unknown'}


def test_active_session_is_recognised_from_the_ros_graph(server, active_run):
    """A running session becomes active when its run-scoped ROS names appear, and not otherwise."""
    wait_until(lambda: session(server, ACTIVE)['activity'] == 'active', message='the active run was not recognised')
    active = session(server, ACTIVE)
    assert active['ros']['listen_status_topics'] == ['/run_' + ACTIVE.replace('-', '_') + '/sense/hear/listen/_action/status']
    assert session(server, '66666666-6666-4666-8666-666666666666')['activity'] == 'running_not_visible'


def test_live_listen_status_is_readable_through_the_echo_stream(server, active_run):
    """The Listen status topic alternates between executing and finished, as SMPA's action server would report."""
    from websockets.sync.client import connect
    topic = session(server, ACTIVE)['ros']['listen_status_topics'][0]
    states = set()
    with connect(server.replace('http', 'ws') + '/api/v1/ros/topics/echo?name=' + quote(topic, safe='')
                 + '&type=action_msgs/msg/GoalStatusArray&rate=4') as socket:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and states != {2, 4}:
            message = json.loads(socket.recv(timeout=10))
            if message['kind'] == 'data':
                states.update(item['status'] for item in message['message']['status_list'])
    assert states == {2, 4}


def test_observations_and_files(server):
    """Observations come in time order; recorded images and audio are served with their media types."""
    result = fetch(server + f'{API}/sessions/{ROBOT}/observations')
    entries = result['observations']
    assert len(entries) == 7 and result['contract_version'] == '1.7' and result['revision']
    image = next(entry for entry in entries if entry['kind'] == 'scene_description')['image']
    with urlopen(server + image, timeout=5) as response:
        assert response.headers['Content-Type'] == 'image/png' and response.read()[:4] == b'\x89PNG'
    audio = next(entry for entry in entries if entry['kind'] == 'audio_transcript')['audio']
    with urlopen(Request(server + audio, headers={'Range': 'bytes=0-11'}), timeout=5) as response:
        assert response.status == 206 and response.read() == b'RIFF' + (48 + 32000).to_bytes(4, 'little') + b'WAVE'
    with urlopen(server + audio, timeout=5) as response:
        assert response.headers['Content-Type'] == 'audio/wav' and response.read()[8:12] == b'WAVE'
    assert [entry['modality'] for entry in fetch(server + f'{API}/sessions/{ROBOT}/observations?modality=read')['observations']] == ['read', 'read']


@pytest.mark.parametrize('path', ['../x', 'metadata.yaml', '/etc/passwd', 'observations/vision/missing.png'])
def test_files_outside_recorded_media_are_refused(server, path):
    """Paths leaving the session, non-media files and missing files give 404."""
    with pytest.raises(HTTPError) as refused:
        urlopen(server + f'{API}/sessions/{ROBOT}/files?path=' + quote(path, safe=''), timeout=5)
    assert refused.value.code == 404


def test_unknown_sessions_and_bad_parameters(server):
    """Unknown sessions give 404; an unknown modality filter gives 422."""
    for path in ('/sessions/nope', '/sessions/nope/observations'):
        with pytest.raises(HTTPError) as missing:
            fetch(server + API + path)
        assert missing.value.code == 404
    with pytest.raises(HTTPError) as invalid:
        fetch(server + f'{API}/sessions/{ROBOT}/observations?modality=smell')
    assert invalid.value.code == 422


def test_reading_through_the_server_changes_nothing(server):
    """Every SMPA read route is read-only: the memory directory is identical before and after use.

    The only other SMPA routes are the conversation routes, which relay to
    SMPA's own conversation server (tested in test_conversation.py), and the
    Dialogue routes, which call SMPA's ROS 2 stage nodes (tested in
    test_dialogue_ros.py) and never write the memory directory themselves.
    """
    before = tree_fingerprint(smpa_memory())
    for item in fetch(server + API + '/sessions')['sessions']:
        for entry in fetch(server + f"{API}/sessions/{item['session_id']}/observations")['observations']:
            for key in ('image', 'audio'):
                if entry[key]:
                    urlopen(server + entry[key], timeout=5).read()
    assert tree_fingerprint(smpa_memory()) == before
    routes = fetch(server + '/api/openapi.json')['paths']
    assert all(set(methods) == {'get'} for path, methods in routes.items()
               if path.startswith('/api/v1/smpa') and not path.startswith(('/api/v1/smpa/conversation/',
                                                                              '/api/v1/smpa/dialogue/')))


def test_unconfigured_and_demonstration_memory(ros_environment, tmp_path):
    """Without a memory directory the SMPA routes explain it; a demo directory is flagged as such."""
    from conftest import build_memory
    environment = dict(ros_environment, SMPA_GUI_MEMORY_ROOT=str(tmp_path / 'absent'))
    process, base = start_server(environment)
    try:
        assert fetch(base + API + '/status')['configured'] is False
        with pytest.raises(HTTPError) as missing:
            fetch(base + API + '/sessions')
        assert missing.value.code == 404 and 'memory-root' in json.load(missing.value)['detail']
        assert fetch(base + '/api/v1/health')['status'] == 'ok'
    finally:
        stop(process)
    demo = build_memory(tmp_path / 'demo', demo=True)
    process, base = start_server(dict(ros_environment, SMPA_GUI_MEMORY_ROOT=str(demo)))
    try:
        assert fetch(base + API + '/status')['demonstration'] is True
        assert fetch(base + API + '/sessions')['demonstration'] is True
    finally:
        stop(process)


def test_smpa_integration_can_be_disabled(ros_environment):
    """With --no-smpa (SMPA_GUI_NO_SMPA=1) the SMPA routes say so and read nothing; ROS routes keep working."""
    environment = dict(ros_environment, SMPA_GUI_NO_SMPA='1')
    process, base = start_server(environment)
    try:
        status = fetch(base + API + '/status')
        assert status['enabled'] is False and status['configured'] is False and status['memory_root'] is None
        with pytest.raises(HTTPError) as disabled:
            fetch(base + API + '/sessions')
        assert disabled.value.code == 404 and 'disabled' in json.load(disabled.value)['detail']
        assert fetch(base + '/api/v1/health')['status'] == 'ok'
        assert fetch(base + '/api/v1/ros/services')['services'] is not None
    finally:
        stop(process)


def test_ros_side_imports_nothing_from_smpa():
    """The ROS adapter and the server import no SMPA module; only the GUI's own read-only SMPA adapter reads SMPA files."""
    import ast
    for path in (GUI / 'smpa_gui').rglob('*.py'):
        tree = ast.parse(path.read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not any(name.split('.')[0] in ('smpa_ros', 'smpa_llm', 'memory', 'runner', 'sense', 'speech') for name in imported), path
