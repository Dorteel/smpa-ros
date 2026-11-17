"""Check the installed package boundary without loading inference or training code."""
from importlib.metadata import distribution
from pathlib import Path
from xml.etree import ElementTree

from ament_index_python.packages import get_package_share_directory
import yaml
from packaging.requirements import Requirement


def test_functional_packages_install_primary_configuration():
    """Find each functional package through ROS and compare its inference defaults.

    Read installed resources rather than checkout paths. The new shared package
    must expose the existing model and provider defaults without acquiring any
    cognitive stage configuration or changing the active robot configuration.
    """
    cognitive = Path(get_package_share_directory('smpa_ros'))
    shared = Path(get_package_share_directory('smpa_llm'))
    from smpa_llm.configuration import load_configuration
    pipeline = load_configuration(cognitive / 'config.yaml')
    cognitive_defaults = yaml.safe_load((cognitive / 'config.yaml').read_text())
    assert not {'model', 'openai', 'nebula'} & cognitive_defaults.keys()
    inference = yaml.safe_load((shared / 'config.yaml').read_text())
    assert inference == {key: pipeline[key] for key in ('model', 'openai', 'nebula')}
    assert pipeline['stage_settings']['plan']['response_format'] == 'json_schema'
    assert pipeline['conversation']['agent'] == 'local:qwen3-4b'


def test_shared_package_installs_without_an_inference_process():
    """Import the installed shared package and inspect its distribution metadata.

    The shared implementation must import without loading weights or registering
    an inference executable. Its declared dependencies remain separate from
    training requirements. Existing execution stays with the cognitive package.
    """
    import smpa_llm
    assert Path(smpa_llm.__file__).is_file()
    installed = distribution('smpa-llm')
    assert {entry.name for entry in installed.entry_points} == {'download_models'}
    requirements = [Requirement(value) for value in installed.requires]
    assert {value.name for value in requirements} == {
        'setuptools', 'outlines', 'jsonschema', 'openai', 'python-dotenv'}
    assert str(requirements[0].specifier) == '<80,>=30.3'
    cognitive = Path(get_package_share_directory('smpa_ros'))
    metadata = ElementTree.parse(cognitive / 'package.xml').getroot()
    assert 'smpa_llm' in [item.text for item in metadata.findall('exec_depend')]


def test_interface_package_remains_configuration_free():
    """Find generated interfaces in their original package without adding settings.

    Import the existing service and action types to confirm their Python API is
    retained. An interface-only package must not receive a meaningless primary
    configuration file as a consequence of the functional-package convention.
    """
    from smpa_ros_interfaces.action import Listen, Talk
    from smpa_ros_interfaces.srv import DescribeScene, EvaluateVisualDecision, SegmentImage
    assert all(interface is not None for interface in
               (Listen, Talk, DescribeScene, EvaluateVisualDecision, SegmentImage))
    interfaces = Path(get_package_share_directory('smpa_ros_interfaces'))
    assert not (interfaces / 'config.yaml').exists()


def test_legacy_backend_imports_preserve_module_identity():
    """Compare legacy and shared imports without constructing any backend.

    Both paths must name one module so existing monkeypatches and injected
    constructors affect the implementation used by cognition. This preserves
    compatibility without copying engines or loading weights during import.
    """
    from importlib import import_module
    for name in ('api', 'ollama', 'transformers', 'visual_jev'):
        legacy = import_module('smpa_ros.backends.' + name)
        shared = import_module('smpa_llm.inference.backends.' + name)
        assert legacy is shared


def test_inference_package_does_not_import_cognition():
    """Inspect installed inference sources for reverse package dependencies.

    Parse each Python module and reject direct cognitive imports. Planning
    schemas and transcript persistence remain cognitive responsibilities;
    generic engines receive their request contracts as arguments instead.
    """
    import ast
    import smpa_llm.inference
    root = Path(smpa_llm.inference.__file__).parent
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                     [item.name for item in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name == 'smpa_ros' or name.startswith('smpa_ros.') for name in names), path


def test_configuration_legacy_and_external_override_contract(tmp_path):
    """Resolve external defaults without changing legacy YAML or override meaning.

    Legacy files remain complete configurations. Explicit section overrides
    replace their section, relative defaults resolve beside the chosen file,
    and loading changes neither file. Reject inference files containing
    cognitive settings rather than assigning them an ambiguous owner.
    """
    from smpa_llm.configuration import load_configuration
    defaults = tmp_path / 'inference.yaml'
    defaults.write_text('model: {backend: transformers, device: auto}\n')
    path = tmp_path / 'config.yaml'
    legacy = 'model: {backend: ollama}\ncamera_topic: /camera\n'
    path.write_text(legacy)
    assert load_configuration(path) == yaml.safe_load(legacy)
    selected = 'inference_config: inference.yaml\nmodel: {backend: api}\n'
    path.write_text(selected)
    assert load_configuration(path) == {'model': {'backend': 'api'}}
    assert path.read_text() == selected
    defaults.write_text('stage_settings: {}\n')
    import pytest
    with pytest.raises(ValueError, match='only model and provider'):
        load_configuration(path)
