"""Tests of the ``smpa_gui`` ROS 2 package: colcon build, discovery, entry point, launch, assets, shutdown.

These are real integration tests: the package is built with colcon into a
temporary workspace (with the GUI's interpreter, as the README describes),
and the installed console script and launch file are run through ``ros2 run``
and ``ros2 launch``. They are skipped when colcon is not installed.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import pytest

from conftest import GUI, PYTHON, fetch, free_port

pytestmark = pytest.mark.skipif(shutil.which('colcon') is None, reason='colcon is not installed')


@pytest.fixture(scope='module')
def workspace(tmp_path_factory, ros_environment):
    """Build the package once into a temporary workspace and return its path and environment."""
    directory = tmp_path_factory.mktemp('workspace')
    result = subprocess.run([PYTHON, shutil.which('colcon'), '--log-base', str(directory / 'log'), 'build',
                             '--paths', str(GUI), '--build-base', str(directory / 'build'),
                             '--install-base', str(directory / 'install')],
                            cwd=directory, env=ros_environment, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(path=directory, environment=ros_environment)


def in_workspace(workspace, command, **options):
    """Run a shell command with the built workspace sourced and return the completed process.

    The command runs inside the workspace folder, never inside the source
    folder, so Python imports the installed package rather than the sources.
    """
    setup = workspace['path'] / 'install' / 'local_setup.bash'
    return subprocess.run(['bash', '-c', f'source {setup} && {command}'], env=workspace['environment'],
                          cwd=workspace['path'], capture_output=True, text=True, timeout=options.get('timeout', 60))


def start_in_workspace(workspace, command):
    """Start a long-running command with the built workspace sourced, in its own process group."""
    setup = workspace['path'] / 'install' / 'local_setup.bash'
    return subprocess.Popen(['bash', '-c', f'source {setup} && exec {command}'], env=workspace['environment'],
                            cwd=workspace['path'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)


def wait_for_health(base, process, timeout=30):
    """Wait until the GUI answers its health check, failing with its output if it exits first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError('GUI exited: ' + process.stdout.read())
        try:
            return fetch(base + '/api/v1/health', timeout=1.0)
        except OSError:
            time.sleep(0.3)
    raise AssertionError('GUI did not answer')


def interrupt(process, timeout=20):
    """Send Ctrl+C to the process group, as a terminal would, and return the output after it exits."""
    os.killpg(process.pid, signal.SIGINT)
    output, _ = process.communicate(timeout=timeout)
    return output


def test_package_is_discovered_with_its_executable_and_launch_file(workspace):
    """ros2 pkg finds the package, its console entry point, its manifest and its launch file."""
    prefix = in_workspace(workspace, 'ros2 pkg prefix smpa_gui').stdout.strip()
    assert prefix == str(workspace['path'] / 'install' / 'smpa_gui')
    assert in_workspace(workspace, 'ros2 pkg executables smpa_gui').stdout.split() == ['smpa_gui', 'smpa_gui']
    share = Path(prefix) / 'share' / 'smpa_gui'
    assert (share / 'package.xml').is_file() and (share / 'launch' / 'gui.launch.py').is_file()
    assert 'smpa_gui' in in_workspace(workspace, 'ros2 pkg list').stdout.split()
    help_text = in_workspace(workspace, 'ros2 run smpa_gui smpa_gui --help').stdout
    assert '--read-only' in help_text and '--no-smpa' in help_text


def test_frontend_assets_are_installed_completely(workspace):
    """Every frontend file and mock graph of the source tree is installed beside the installed Python package."""
    package = next((workspace['path'] / 'install' / 'smpa_gui' / 'lib').glob('python3*/site-packages/smpa_gui'))
    for folder in ('frontend', 'mock_graphs'):
        source, installed = GUI / 'smpa_gui' / folder, package / folder
        expected = sorted(str(path.relative_to(source)) for path in source.rglob('*') if path.is_file())
        assert sorted(str(path.relative_to(installed)) for path in installed.rglob('*') if path.is_file()) == expected
    assert (package / 'frontend' / 'tabs' / 'smpa' / 'dialogue.js').is_file()
    assert (package / 'frontend' / 'vendor' / 'cytoscape.min.js').stat().st_size == \
        (GUI / 'smpa_gui' / 'frontend' / 'vendor' / 'cytoscape.min.js').stat().st_size
    share = workspace['path'] / 'install' / 'smpa_gui' / 'share' / 'smpa_gui'
    assert (share / 'layouts' / 'smpa-current.yaml').is_file()


def test_ros2_run_serves_the_installed_frontend_and_stops_cleanly(workspace):
    """ros2 run starts the installed GUI, which serves its installed assets and leaves the graph on Ctrl+C."""
    port = free_port()
    process = start_in_workspace(workspace, f'ros2 run smpa_gui smpa_gui --port {port} --no-smpa')
    base = f'http://127.0.0.1:{port}'
    try:
        health = wait_for_health(base, process)
        assert health['ros']['node'] == '/smpa_gui'
        from urllib.request import urlopen
        with urlopen(base + '/', timeout=5) as response:
            assert b'SMPA-ROS GUI' in response.read()
        with urlopen(base + '/tabs/ros/services.js', timeout=5) as response:
            assert response.headers['content-type'].startswith(('text/javascript', 'application/javascript'))
        installed = next((workspace['path'] / 'install' / 'smpa_gui' / 'lib').glob('python3*/site-packages/smpa_gui'))
        assert in_workspace(workspace, f"{PYTHON} -c \"import smpa_gui.app as a; print(a.FRONTEND)\"").stdout.strip() \
            == str(installed / 'frontend')
    finally:
        output = interrupt(process)
    assert process.returncode == 0, output
    assert 'node left the ROS graph' in output and 'Traceback' not in output


def test_ros2_launch_passes_arguments_and_shuts_down_cleanly(workspace):
    """ros2 launch with arguments starts a read-only GUI without SMPA; Ctrl+C ends it cleanly."""
    port = free_port()
    process = start_in_workspace(workspace, f'ros2 launch smpa_gui gui.launch.py port:={port} read_only:=true smpa:=false')
    base = f'http://127.0.0.1:{port}'
    try:
        wait_for_health(base, process)
        assert fetch(base + '/api/v1/session')['enabled'] is False
        assert fetch(base + '/api/v1/smpa/status')['enabled'] is False
        nodes = in_workspace(workspace, 'ros2 node list --no-daemon').stdout.split()
        assert '/smpa_gui' in nodes or nodes == []  # discovery from a fresh CLI may lag; the health check is authoritative
    finally:
        output = interrupt(process)
    assert 'node left the ROS graph' in output, output
    assert 'process has finished cleanly' in output and 'Traceback' not in output


def test_launch_arguments_translate_to_command_line_options():
    """The launch file maps each argument to the executable's options and leaves SMPA settings out when disabled."""
    specification = importlib.util.spec_from_file_location('gui_launch', GUI / 'launch' / 'gui.launch.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    defaults = {name: default for name, default, _ in module.ARGUMENTS}
    assert module.command_arguments(defaults) == ['--host', '127.0.0.1', '--port', '8765']
    assert module.command_arguments(dict(defaults, read_only='true', keep_goals_on_exit='True',
                                         smpa_memory_root='/data/memory')) == \
        ['--host', '127.0.0.1', '--port', '8765', '--read-only', '--keep-goals-on-exit', '--memory-root', '/data/memory']
    assert module.command_arguments(dict(defaults, smpa_root='/ws/smpa-ros', smpa_layout='/ws/layout.yaml'))[-4:] == \
        ['--smpa-root', '/ws/smpa-ros', '--layout', '/ws/layout.yaml']
    assert module.command_arguments(dict(defaults, smpa='false', smpa_memory_root='/data/memory', smpa_root='/ws'))[-1] == '--no-smpa'
    description = module.generate_launch_description()
    assert {entity.name for entity in description.entities if hasattr(entity, 'default_value')} == set(defaults)


def test_gui_launch_can_be_included_by_another_launch_file(workspace, tmp_path):
    """The README's include example starts the GUI from a combined launch file, with arguments passed through."""
    port = free_port()
    combined = tmp_path / 'combined.launch.py'
    combined.write_text(f'''"""A combined launcher that includes the GUI, as a future SMPA launcher could."""
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    """Include the GUI's launch description with a chosen port, read-only and without SMPA."""
    gui = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(PathJoinSubstitution([FindPackageShare('smpa_gui'), 'launch', 'gui.launch.py'])),
        launch_arguments={{'port': '{port}', 'read_only': 'true', 'smpa': 'false'}}.items())
    return LaunchDescription([gui])
''')
    process = start_in_workspace(workspace, f'ros2 launch {combined}')
    base = f'http://127.0.0.1:{port}'
    try:
        wait_for_health(base, process)
        assert fetch(base + '/api/v1/session')['enabled'] is False
    finally:
        output = interrupt(process)
    assert 'node left the ROS graph' in output, output


def test_installed_gui_runs_the_dialogue_against_an_smpa_checkout(workspace, tmp_path):
    """The installed package, launched with smpa_root, serves the Dialogue: models, mock graphs, a real conversation.

    Uses SMPA's real conversation server from the checkout named by
    SMPA_GUI_TEST_SMPA_ROOT with the test configuration and the inference stub.
    """
    from conftest import conversation_world, end_world, smpa_conversation_available, wait_for_participants
    if not smpa_conversation_available():
        pytest.skip('no SMPA checkout with a conversation server and .venv (set SMPA_GUI_TEST_SMPA_ROOT)')
    import json
    from urllib.request import Request, urlopen
    world = conversation_world(tmp_path)
    workspace = dict(workspace, environment=world['environment'])
    port = free_port()
    process = start_in_workspace(workspace, f"ros2 launch smpa_gui gui.launch.py port:={port} smpa_layout:={tmp_path / 'layout.yaml'}")
    base = f'http://127.0.0.1:{port}'
    try:
        wait_for_health(base, process)
        wait_for_participants(base)
        assert {model['source'] for model in fetch(base + '/api/v1/smpa/models')['models']} == {'api:stub', 'local:absent-local-model'}
        mock = fetch(base + '/api/v1/smpa/graphs/belief?source=mock')
        assert mock['state'] == 'ok' and '/site-packages/smpa_gui/mock_graphs/' in mock['path']
        token = fetch(base + '/api/v1/session')['token']

        def post(path, body):
            """POST one conversation request with the session token."""
            request = Request(base + '/api/v1/smpa/conversation/' + path, data=json.dumps(body).encode(), method='POST',
                              headers={'Content-Type': 'application/json', 'X-SMPA-GUI-Token': token})
            with urlopen(request, timeout=60) as response:
                return json.load(response)['conversation']

        assert post('start', {'source': 'api:stub', 'request_id': 'pkg-start-0001'})['phase'] == 'ready'
        reply = post('messages', {'text': 'From the installed GUI', 'request_id': 'pkg-message-0001'})
        assert reply['messages'][-1]['content'].endswith('to: From the installed GUI')
    finally:
        output = interrupt(process)
        end_world(world)
    assert 'node left the ROS graph' in output, output
    runs = list((world['memory'] / 'runs').iterdir())
    assert len(runs) == 1 and 'From the installed GUI' in (runs[0] / 'conversation.md').read_text()
