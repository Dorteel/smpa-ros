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
