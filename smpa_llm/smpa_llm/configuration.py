"""Read package-owned inference defaults while retaining legacy complete YAML files."""
from copy import deepcopy
from pathlib import Path
import yaml


def load_configuration(path):
    """Read one configuration and its explicitly selected inference defaults.

    A legacy YAML file without inference_config is returned with its existing
    semantics. A package-owned cognitive file selects inference defaults through
    package://smpa_llm/config.yaml or an external YAML path relative to itself.
    Resolve the package resource with ament, then overlay explicit top-level
    values from the selected file. An override replaces its entire section,
    matching the old YAML contract; no implicit recursive merging is introduced.
    Remove the resource selector from the effective configuration so persisted
    run snapshots retain their established format. Never write either file.
    """
    path = Path(path).expanduser()
    configuration = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(configuration, dict):
        raise ValueError('Configuration must be a YAML mapping')
    selected = configuration.pop('inference_config', None)
    if selected is None:
        return configuration
    if selected == 'package://smpa_llm/config.yaml':
        from ament_index_python.packages import get_package_share_directory
        defaults_path = Path(get_package_share_directory('smpa_llm')) / 'config.yaml'
    elif isinstance(selected, str) and not selected.startswith('package://'):
        defaults_path = Path(selected).expanduser()
        if not defaults_path.is_absolute():
            defaults_path = path.resolve().parent / defaults_path
    else:
        raise ValueError('inference_config must select smpa_llm/config.yaml or an external YAML file')
    defaults = yaml.safe_load(defaults_path.read_text(encoding='utf-8'))
    if not isinstance(defaults, dict) or set(defaults) - {'model', 'openai', 'nebula'}:
        raise ValueError('Inference defaults may contain only model and provider settings')
    return dict(deepcopy(defaults), **configuration)
