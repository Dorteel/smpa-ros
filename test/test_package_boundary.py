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
    pipeline = yaml.safe_load((cognitive / 'config.yaml').read_text())
    inference = yaml.safe_load((shared / 'config.yaml').read_text())
    assert inference == {key: pipeline[key] for key in ('model', 'openai', 'nebula')}
    assert pipeline['stage_settings']['plan']['response_format'] == 'json_schema'
    assert pipeline['conversation']['agent'] == 'local:qwen3-4b'


def test_shared_package_installs_without_an_inference_process():
    """Import the installed shared package and inspect its distribution metadata.

    Its first migration step must expose a real Python package without loading
    weights, registering a new executable, or requiring inference and training
    dependencies. Existing execution remains owned by the cognitive package.
    """
    import smpa_llm
    assert Path(smpa_llm.__file__).is_file()
    installed = distribution('smpa-llm')
    assert not installed.entry_points
    requirements = [Requirement(value) for value in installed.requires]
    assert len(requirements) == 1 and requirements[0].name == 'setuptools'
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
