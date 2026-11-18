"""Check cognitive launch descriptions without starting ROS nodes."""
import importlib.util
from pathlib import Path

from launch import LaunchContext


ROOT = Path(__file__).resolve().parents[1]


def load_launch(name):
    """Load a repository launch file directly for lightweight inspection.

    The source path avoids relying on an older installed workspace overlay.
    Loading a launch description does not start nodes or inference.
    """
    path = ROOT / 'smpa_ros' / 'launch' / name
    spec = importlib.util.spec_from_file_location('fixture_' + name.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standalone_launch_contains_four_independent_stages(monkeypatch):
    """Expose each cognitive action server without starting an experiment.

    Replace launch Node construction with dictionaries and inspect the real
    description's four stage executables and shared parameters.
    """
    module = load_launch('cognitive_stages.launch.py')
    monkeypatch.setattr(module, 'Node', lambda **kwargs: kwargs)
    description = module.generate_launch_description()
    nodes = [entity for entity in description.entities if isinstance(entity, dict)]
    assert [node['executable'] for node in nodes] == ['sense_stage', 'model_stage', 'plan_stage', 'act_stage']
    assert all(set(node['parameters'][0]) == {'memory_root', 'inference_node', 'timeout'} for node in nodes)


def test_existing_experiment_launch_can_start_or_reuse_stages(monkeypatch):
    """Keep the normal launcher usable with its own or existing stage servers.

    Inspect both settings without starting ROS or model processes. The four
    stage nodes share the experiment's memory root; disabling them retains the
    original single experiment process when servers already exist.
    """
    module = load_launch('llm.launch.py')
    monkeypatch.setattr(module, 'Node', lambda **kwargs: kwargs)
    context = LaunchContext()
    context.launch_configurations.update(service='vision', participants_only='false', start_stage_nodes='true',
        config='/tmp/config.yaml', world_model_root='/tmp/run-memory', run_name='run', python='python3',
        model_root='/tmp/models', audio_lock='', input_device='', output_device='', demo='false', timeout='',
        hear='', vision='', read_session='', max_cycles='')
    nodes = module.start(context)
    assert [node['executable'] for node in nodes] == ['sense_stage', 'model_stage', 'plan_stage', 'act_stage', 'smpa']
    assert all(node['parameters'][0]['memory_root'] == '/tmp/run-memory' for node in nodes[:4])
    context.launch_configurations['start_stage_nodes'] = 'false'
    assert [node['executable'] for node in module.start(context)] == ['smpa']
