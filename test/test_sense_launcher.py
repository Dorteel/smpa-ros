"""Verify the repository-owned all-modality launcher without starting robot actions."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml
from smpa_ros.conversation import open_conversation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher(tmp_path):
    """Exercise the real shell script with a harmless ROS launch command sink.

    Create the expected workspace layout and retain real Python and Read-source validation. Stub the unrelated Webots type
    preflight and ros2 command so no
    robot process starts, recording its final argument vector for inspection.
    Return the checkout, selected conversation and command capture paths.
    """
    workspace = tmp_path / 'workspace'
    repo = workspace / 'src/smpa-ros'
    repo.mkdir(parents=True)
    (repo / 'run_smpa.sh').write_bytes((ROOT / 'run_smpa.sh').read_bytes())
    python = repo / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/env bash\n'
                      'if [[ $# == 1 && $1 == - ]]; then cat >/dev/null; exit 0; fi\n'
                      'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o755)
    install = workspace / 'install'
    for package in ('smpa_ros', 'smpa_ros_interfaces'):
        share = install / package / 'share' / package
        share.mkdir(parents=True)
        (share / 'package.xml').write_text('<package/>')
    launch = install / 'smpa_ros/share/smpa_ros/launch'
    launch.mkdir()
    (launch / 'llm.launch.py').write_bytes((ROOT / 'launch/llm.launch.py').read_bytes())
    commands = tmp_path / 'commands'
    commands.mkdir()
    capture = tmp_path / 'arguments.json'
    ros2 = commands / 'ros2'
    ros2.write_text('#!/usr/bin/env python3\n' +
                   'import json,sys\nfrom pathlib import Path\n' +
                   f'install=Path({str(install)!r})\n' +
                   "if sys.argv[1:3]==['pkg','prefix']:print(install/sys.argv[3])\n" +
                   f'else:Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n')
    ros2.chmod(0o755)
    (install / 'setup.bash').write_text('export PATH=' + shlex.quote(str(commands)) + ':"$PATH"\n' +
                                         'export PYTHONPATH=' + shlex.quote(os.environ.get('PYTHONPATH', '')) + '\n')
    source = open_conversation(tmp_path / 'conversation sessions')
    return repo, source, capture


def invoke(launcher, *arguments):
    """Run the real shell launcher and preserve startup diagnostics for assertions."""
    repo, _, _ = launcher
    return subprocess.run(['bash', str(repo / 'run_smpa.sh'), *map(str, arguments)],
                          text=True, capture_output=True, timeout=20)


def test_all_senses_launcher_and_explicit_session(launcher):
    """Forward all three switches and a spaced absolute session path exactly once."""
    repo, source, capture = launcher
    before = {path: path.read_bytes() for path in source.directory.rglob('*') if path.is_file()}
    result = invoke(launcher, '--all-senses', source.directory, 'max_cycles:=2', 'run_name:=test')
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = json.loads(capture.read_text())
    assert arguments[:3] == ['launch', 'smpa_ros', 'llm.launch.py']
    for argument in ('service:=all', 'hear:=true', 'vision:=true',
                     f'read_session:={source.directory}', 'max_cycles:=2'):
        assert arguments.count(argument) == 1
    assert 'service:=vision' not in arguments and 'max_cycles:=1' not in arguments
    assert f'python:={repo}/.venv/bin/python' in arguments
    assert {path: path.read_bytes() for path in source.directory.rglob('*') if path.is_file()} == before


@pytest.mark.parametrize('conflict', ['service:=vision', 'hear:=false', 'vision:=false',
                                    'read_session:=/other', 'participants_only:=true'])
def test_all_senses_rejects_conflicting_overrides(launcher, conflict):
    """Reject contradictory requests before dispatching any ROS launch command."""
    _, source, capture = launcher
    result = invoke(launcher, '--all-senses', source.directory, conflict)
    assert result.returncode != 0 and 'owns service' in result.stderr
    assert not capture.exists()


def test_missing_and_nonconversation_sources_fail_before_launch(launcher, tmp_path):
    """Require a selected valid conversation without creating or repairing one."""
    _, _, capture = launcher
    assert invoke(launcher, '--all-senses').returncode != 0
    assert invoke(launcher, '--all-senses', tmp_path / 'missing').returncode != 0
    result = invoke(launcher, '--all-senses', tmp_path)
    assert result.returncode != 0 and 'Cannot start all sensing modalities' in result.stderr
    assert not capture.exists()


def test_legacy_launcher_selection_stays_unchanged(launcher):
    """Keep ordinary vision defaults and explicit legacy service selection intact."""
    _, _, capture = launcher
    result = invoke(launcher, 'service:=speech', 'max_cycles:=3')
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = json.loads(capture.read_text())
    assert arguments.count('service:=speech') == 1
    assert not any(value.startswith('read_session:=') for value in arguments)
    assert 'service:=vision' not in arguments and 'max_cycles:=1' not in arguments


def test_nonphysical_profile_preserves_existing_action_schema():
    """Use the canonical Observe schema and one unchanged cognitive model choice."""
    from smpa_ros.backends.base import stage_generation
    from smpa_ros.plan import action_schema, enabled_actions
    config = yaml.safe_load((ROOT / 'config/sense_observe.yaml').read_text())
    original = yaml.safe_load((ROOT / 'config.yaml').read_text())
    assert config['model'] == original['model']
    assert list(enabled_actions(config)) == ['observe']
    assert stage_generation(config, 'plan')['output_schema'] == action_schema(['observe'])
    assert config['camera_topic'] == original['camera_topic']
    subprocess.run(['bash', '-n', str(ROOT / 'run_smpa.sh')], check=True)


def test_svg_contains_only_discovered_topic_endpoints():
    """Keep file-backed Read and services isolated rather than inventing edges."""
    from scripts.capture_sense_graph import topic_dot
    prefix = '/run_test/sense'
    inventory = dict(nodes=[{'name': prefix + '/read/read'},
                            {'name': prefix + '/vision/sense',
                             'services': [['/description', ['DescribeScene']]]}],
                     topics=[dict(name='/tiago/camera/color/image_raw', types=['sensor_msgs/msg/Image'],
                                  publishers=[{'node': '/camera'}],
                                  subscriptions=[{'node': prefix + '/vision/sense'}])])
    dot = topic_dot(inventory)
    edges = [line for line in dot.splitlines() if ' -> ' in line]
    assert len(edges) == 2
    assert prefix + '/read/read' in dot
    assert all('/read/read' not in edge for edge in edges)
    assert '/description' not in dot and 'conversation.md' not in dot
    assert 'source' not in '\n'.join(edges)


def test_svg_exports_actual_graphviz_vector(tmp_path):
    """Validate the actual SVG exporter without adding a new runtime dependency."""
    import shutil
    from xml.etree import ElementTree
    from scripts.capture_sense_graph import topic_dot
    if shutil.which('dot') is None:
        pytest.skip('Graphviz is unavailable; installed rqt_graph environments normally provide it')
    dot = tmp_path / 'runtime.dot'
    dot.write_text(topic_dot(dict(nodes=[{'name': '/run_test/sense/read/read'}], topics=[])))
    svg = tmp_path / 'runtime.svg'
    subprocess.run(['dot', '-Tsvg', str(dot), '-o', str(svg)], check=True)
    tree = ElementTree.parse(svg)
    assert tree.getroot().tag == '{http://www.w3.org/2000/svg}svg'
    assert '/run_test/sense/read/read' in svg.read_text()


def test_relative_session_is_resolved_before_changing_directory(launcher, monkeypatch):
    """Bind a caller-relative session before launcher setup changes working directories."""
    _, source, capture = launcher
    monkeypatch.chdir(source.directory.parent)
    result = invoke(launcher, '--all-senses', source.directory.name)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'read_session:={source.directory}' in json.loads(capture.read_text())
