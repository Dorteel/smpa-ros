"""Start real ROS 2 fixture nodes and a real GUI server for the tests.

Concepts:
    The tests run in their own ROS domain, chosen at random and limited to
    this computer, so they neither see nor disturb other ROS systems. The
    fixture process and the GUI server are separate processes, exactly as in
    real use. ``GUI_TEST_PYTHON`` names the interpreter that has rclpy and
    the GUI packages; it defaults to the interpreter running pytest.
"""
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import time

import pytest

GUI = Path(__file__).resolve().parent.parent
PYTHON = os.environ.get('GUI_TEST_PYTHON', sys.executable)


def ros_available():
    """Tell whether the chosen interpreter can import rclpy and the web stack."""
    check = subprocess.run([PYTHON, '-c', 'import rclpy, fastapi, uvicorn, example_interfaces, sensor_msgs'],
                           capture_output=True)
    return check.returncode == 0


def free_port():
    """Ask the operating system for a TCP port that is free right now."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def fetch(url, timeout=5.0):
    """Fetch a URL with the standard library and return its parsed JSON body."""
    import json
    from urllib.request import urlopen
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def start_server(environment):
    """Start a GUI server process on a free port and wait until it answers.

    Return the process and its base URL. If the server exits or stays silent
    for twenty seconds, fail with its output so the cause is visible.
    """
    port = free_port()
    process = subprocess.Popen([PYTHON, '-m', 'smpa_gui', '--port', str(port)], cwd=GUI, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f'http://127.0.0.1:{port}'
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('GUI server exited: ' + process.stdout.read())
        try:
            fetch(base + '/api/v1/health', timeout=1.0)
            return process, base
        except OSError:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError('GUI server did not start: ' + process.stdout.read())


def stop(process):
    """Interrupt a process the way Ctrl+C would, and kill it if it lingers."""
    import signal
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


SMPA_MEMORY = None


def build_memory(directory, demo=False):
    """Write the fixture SMPA memory directory with the GUI's interpreter and return its path.

    The builder needs Pillow from the ROS environment, which the interpreter
    running browser tests may not be able to load, so it runs as a subprocess.
    """
    command = [PYTHON, str(GUI / 'test' / 'fixtures' / 'smpa_memory.py'), '--output', str(directory)]
    subprocess.run(command + (['--demo'] if demo else []), check=True, capture_output=True)
    return Path(directory)


def smpa_memory():
    """Build the fixture SMPA memory directory once per test session and return its path."""
    global SMPA_MEMORY
    if SMPA_MEMORY is None:
        import tempfile
        SMPA_MEMORY = str(build_memory(Path(tempfile.mkdtemp(prefix='smpa-memory-')) / 'memory'))
    return SMPA_MEMORY


def isolated_environment():
    """Build an environment with a random ROS domain restricted to this computer.

    The GUI started in this environment reads the fixture SMPA memory directory.
    """
    environment = dict(os.environ)
    environment['SMPA_GUI_MEMORY_ROOT'] = smpa_memory()
    environment['ROS_DOMAIN_ID'] = str(random.randint(110, 200))
    environment['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'
    environment['PYTHONUNBUFFERED'] = '1'
    return environment


@pytest.fixture(scope='session')
def ros_environment():
    """Provide the isolated ROS environment shared by fixtures, server and command-line tools.

    The ros2 command-line tools start a background daemon for the test's ROS
    domain. When the session ends, that daemon is stopped, so repeated test
    runs do not leave one daemon per random domain behind.
    """
    if not ros_available():
        pytest.skip('ROS 2 with rclpy, example_interfaces and the GUI packages is required')
    environment = isolated_environment()
    yield environment
    if shutil.which('ros2'):
        subprocess.run(['ros2', 'daemon', 'stop'], env=environment, capture_output=True, timeout=30)


@pytest.fixture(scope='session')
def fixture_nodes(ros_environment):
    """Run the fixture nodes for the whole test session.

    Wait for the line the fixture process prints when all nodes exist.
    """
    process = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/fixture_nodes.py')], env=ros_environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = process.stdout.readline()
    if 'FIXTURES READY' not in line:
        process.kill()
        raise RuntimeError('Fixture nodes failed to start: ' + line + process.stdout.read())
    yield process
    stop(process)


@pytest.fixture(scope='session')
def server(ros_environment, fixture_nodes):
    """Run one GUI server beside the fixtures and wait until it has discovered them.

    Discovery is asynchronous, so poll the graph until the last fixture node
    and the action are visible. Yield the server's base URL.
    """
    process, base = start_server(ros_environment)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        graph = fetch(base + '/api/v1/ros/graph')
        names = {node['full_name'] for node in graph['nodes']}
        if {'/fixture_talker', '/fixture_sensors', '/fixture_silent'} <= names and graph['actions']:
            break
        time.sleep(0.5)
    else:
        stop(process)
        raise RuntimeError('GUI server never discovered the fixture nodes')
    yield base
    stop(process)


@pytest.fixture()
def lonely_server():
    """Run a GUI server in a ROS domain that contains nothing else.

    This is the situation 'ROS is available but SMPA and all sensors are off'.
    """
    if not ros_available():
        pytest.skip('ROS 2 with rclpy and the GUI packages is required')
    environment = isolated_environment()
    process, base = start_server(environment)
    yield base, environment, process
    if process.poll() is None:
        stop(process)


@pytest.fixture(scope='session')
def browser():
    """Start one headless Chromium for all browser tests, or skip them without Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip('Playwright is not installed')
    (GUI / 'test' / 'artifacts').mkdir(exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(args=['--disable-dev-shm-usage'])
        yield browser
        browser.close()


SMPA_SOURCE = Path(os.environ.get('SMPA_GUI_TEST_SMPA_ROOT') or GUI.parent).resolve()


def smpa_conversation_available():
    """Tell whether SMPA's conversation server and the ROS participants it needs are available for tests.

    Needed: an SMPA checkout with its conversation server and its own
    ``.venv`` (either checkout layout), and, because SMPA's conversation now
    has its Model stage update beliefs over ROS, the installed ``smpa_llm``
    and ``smpa_ros`` packages (SMPA's workspace sourced) with their
    ``llm_server`` and ``model_stage`` executables.
    """
    from smpa_gui.smpa_adapter.smpa_layout import checkout_layout
    if checkout_layout(SMPA_SOURCE) is None or not (SMPA_SOURCE / '.venv' / 'bin' / 'python').exists():
        return False
    if shutil.which('ros2') is None:
        return False
    found = subprocess.run(['ros2', 'pkg', 'executables', 'smpa_ros'], capture_output=True, text=True, timeout=60).stdout
    served = subprocess.run(['ros2', 'pkg', 'executables', 'smpa_llm'], capture_output=True, text=True, timeout=60).stdout
    return 'model_stage' in found and 'llm_server' in served


STUB_ALIAS = 'stub_api'
# The conversation's modelling agent (api:stub) uses SMPA's API defaults; SMPA's conversation server
# requires the inference server's frozen controls to equal them (max_new_tokens 2048 for belief updates).
STUB_CONTROLS = dict(max_new_tokens=2048, temperature=0.7, top_p=0.9, seed=42)


def start_smpa_participants(world):
    """Start SMPA's real inference server and Model stage for a conversation world; return their processes.

    The inference server (``smpa_llm llm_server``) serves one API model,
    the deterministic stub, through SMPA's real API backend; no weights
    load. Its alias is loaded at start (``active_model``) with the
    conversation's modelling controls. The Model stage (``smpa_ros
    model_stage``) uses the world's memory directory. Both run in the
    world's isolated ROS domain, beside the GUI and SMPA's conversation server.
    """
    import yaml
    directory = world['directory']
    (directory / 'llm_config.yaml').write_text(yaml.safe_dump(dict(
        model=dict(model=STUB_ALIAS, model_id='stub-model', backend='api', provider='stub', modality='text'),
        stub=dict(base_url=f"http://127.0.0.1:{world['stub_port']}/v1", api_key_env='SMPA_GUI_STUB_API_KEY',
                  models=dict(text='stub-model', vision='stub-model'), timeout=30))))
    (directory / 'llm_registry.yaml').write_text(yaml.safe_dump(dict(models={})))
    controls = [argument for key, value in STUB_CONTROLS.items()
                for argument in ('-p', f'models.{STUB_ALIAS}.{key}:={value}')]
    log = open(directory / 'participants.log', 'w')
    processes = [
        subprocess.Popen(['ros2', 'run', 'smpa_llm', 'llm_server', '--ros-args',
                          '-p', f"configuration_file:={directory / 'llm_config.yaml'}",
                          '-p', f"registry_file:={directory / 'llm_registry.yaml'}",
                          '-p', f'active_model:={STUB_ALIAS}', *controls],
                         env=world['environment'], stdout=log, stderr=subprocess.STDOUT, start_new_session=True),
        subprocess.Popen(['ros2', 'run', 'smpa_ros', 'model_stage', '--ros-args',
                          '-p', f"memory_root:={world['memory']}", '-p', 'inference_node:=/llm'],
                         env=world['environment'], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)]
    return processes


def end_world(world):
    """Stop everything a conversation world started: SMPA's participants and the inference stub."""
    import signal
    for process in world.get('participants', []):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
    for process in world.get('participants', []):
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    stop(world['stub'])


def wait_for_participants(base, timeout=60.0):
    """Wait until the GUI sees the inference server ready with the stub model and the Model stage's action.

    Uses the GUI's own status route, so this also checks that the GUI
    reaches SMPA's real ROS interfaces.
    """
    deadline = time.monotonic() + timeout
    answer = None
    while time.monotonic() < deadline:
        answer = fetch(base + '/api/v1/smpa/ros', timeout=15)
        if (answer['inference']['status'].get('server_state') == 'ready'
                and answer['inference']['status'].get('active_model') == STUB_ALIAS
                and answer['stages'].get('model', {}).get('available')):
            return answer
        time.sleep(0.5)
    raise RuntimeError(f'SMPA participants did not become ready: {answer}')


def conversation_world(directory, *, agent='api:stub', files=None):
    """Prepare an isolated SMPA world for conversation tests: the inference stub and SMPA's ROS participants.

    Writes, under ``directory``: the test configuration (with the stub's
    port), the test model registry beside it, an empty memory root and a GUI
    layout file pointing at the SMPA checkout. Starts the stub and SMPA's
    inference server and Model stage (``start_smpa_participants``). Nothing
    is written into the SMPA checkout. Returns the processes, the GUI
    environment and the paths; ``end_world`` stops them.
    """
    import yaml
    directory = Path(directory)
    stub_port, conversation_port = free_port(), free_port()
    stub = subprocess.Popen([sys.executable, str(GUI / 'test/fixtures/openai_stub.py'), str(stub_port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert 'STUB READY' in stub.stdout.readline()
    fixture = GUI / 'test' / 'fixtures' / 'conversation'
    config = (fixture / 'config.yaml').read_text().replace('STUB_PORT', str(stub_port))
    if agent != 'api:stub':
        config = config.replace('  agent: api:stub', f'  agent: {agent}')
    (directory / 'models').mkdir(parents=True, exist_ok=True)
    (directory / 'config.yaml').write_text(config)
    (directory / 'models' / 'model_library.yaml').write_text((fixture / 'models' / 'model_library.yaml').read_text())
    (directory / 'memory' / 'runs').mkdir(parents=True, exist_ok=True)
    layout = dict(smpa_root=str(SMPA_SOURCE), python=str(SMPA_SOURCE / '.venv/bin/python'),
                  config=str(directory / 'config.yaml'), model_root=str(directory / 'models'),
                  memory_root=str(directory / 'memory'), conversation_port=conversation_port)
    if files:
        layout['files'] = files
    (directory / 'layout.yaml').write_text(yaml.safe_dump(layout))
    environment = isolated_environment()
    environment.pop('SMPA_GUI_MEMORY_ROOT', None)
    environment['SMPA_GUI_LAYOUT'] = str(directory / 'layout.yaml')
    environment['SMPA_GUI_STUB_API_KEY'] = 'test-only-key'
    world = dict(stub=stub, environment=environment, directory=directory, memory=directory / 'memory',
                 conversation_port=conversation_port, stub_port=stub_port)
    world['participants'] = start_smpa_participants(world)
    return world


@pytest.fixture(scope='module')
def conversation_server(tmp_path_factory):
    """Run a GUI server whose layout points at SMPA's real conversation server and the inference stub.

    Skips when ROS or an SMPA checkout with its ``.venv`` is unavailable
    (set ``SMPA_GUI_TEST_SMPA_ROOT`` to the checkout).
    """
    if not ros_available():
        pytest.skip('ROS 2 with rclpy and the GUI packages is required')
    if not smpa_conversation_available():
        pytest.skip(f'No SMPA checkout with a conversation server and .venv at {SMPA_SOURCE} '
                    '(set SMPA_GUI_TEST_SMPA_ROOT)')
    world = conversation_world(tmp_path_factory.mktemp('conversation'))
    process, base = start_server(world['environment'])
    world.update(base=base, process=process)
    wait_for_participants(base)
    yield world
    if process.poll() is None:
        stop(process)
    end_world(world)
