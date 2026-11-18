"""List the conversation models SMPA is configured with, without loading or contacting any of them.

Concepts:
    SMPA's conversation server offers one agent per local text model in its
    model registry (backends ``transformers`` and ``ollama``) and one per API
    provider in its effective configuration that has ``models.text``,
    ``base_url`` and ``api_key_env``. This module applies the same selection
    rule to the same files so the GUI can show the choices before any server
    runs (starting the server always creates a run, so it is not started just
    to list models). It reads files only and imports nothing from SMPA.

    The effective configuration follows SMPA's ``smpa_llm.configuration``
    rules: a file without ``inference_config`` is complete as it is; a file
    with ``inference_config: package://smpa_llm/config.yaml`` (or a relative
    or absolute YAML path) takes the provider and model defaults from that
    file and overlays its own top-level entries. The registry is
    ``models/model_library.yaml`` beside the configuration, otherwise the
    one installed in ``share/smpa_llm/models`` (before the restructuring:
    ``share/smpa_ros/models``). SMPA finds ``smpa_llm`` through the sourced
    ROS workspace; when it is not installed there, the GUI falls back to the
    checkout's ``smpa_llm`` folder for display and reports that SMPA's server
    will not find it.

    The registry has no description field today. The description shown is
    built from configuration facts only: backend, model id, revision, storage
    path, provider, endpoint and the name of the credential variable. If an
    entry gains a ``description`` (requested from SMPA, see SMPA_CONTRACTS.md
    C8), that text is shown first. Availability
    here is a precheck (weights folder present; credential variable set); the
    conversation server's own verdict replaces it once the server runs.
"""
import os
from pathlib import Path

import yaml


def package_share(name):
    """Return the installed ``share`` folder of a ROS package in the sourced workspace, or None."""
    try:
        from ament_index_python.packages import PackageNotFoundError, get_package_share_directory
    except ImportError:
        return None
    try:
        return Path(get_package_share_directory(name))
    except (PackageNotFoundError, ValueError, LookupError):
        return None


def smpa_llm_folder(smpa_root, problems):
    """Find the folder holding smpa_llm's config.yaml and models/, preferring the installed package.

    SMPA's own server resolves ``package://smpa_llm`` through the sourced
    ROS workspace only. If that fails, the checkout's ``smpa_llm`` folder
    is used here so the models can still be listed, and a problem explains
    that SMPA's server will not start until the workspace is sourced.
    """
    share = package_share('smpa_llm')
    if share is not None:
        return share
    if smpa_root and (Path(smpa_root) / 'smpa_llm' / 'config.yaml').is_file():
        problems.append('The ROS package smpa_llm is not installed in the sourced workspace. Models are listed from '
                        'the checkout; SMPA\'s conversation server needs the SMPA workspace to be built and sourced.')
        return Path(smpa_root) / 'smpa_llm'
    problems.append('The ROS package smpa_llm was not found (source the SMPA workspace).')
    return None


def effective_configuration(config_path, smpa_root, problems):
    """Read SMPA's configuration the way ``smpa_llm.configuration.load_configuration`` does.

    Without ``inference_config`` the file is returned as it is. With it, the
    selected defaults file (smpa_llm's ``config.yaml``, or a YAML path
    relative to the configuration) is read and the configuration's own
    top-level entries replace whole sections of it. The selector itself is
    removed. Raises ``ValueError`` or ``OSError`` for unreadable files.
    """
    configuration = yaml.safe_load(Path(config_path).read_text(encoding='utf-8'))
    if not isinstance(configuration, dict):
        raise ValueError('the configuration is not a mapping')
    selected = configuration.pop('inference_config', None)
    if selected is None:
        return configuration
    if selected == 'package://smpa_llm/config.yaml':
        folder = smpa_llm_folder(smpa_root, problems)
        if folder is None:
            raise ValueError('inference_config selects smpa_llm, which was not found')
        defaults_path = folder / 'config.yaml'
    elif isinstance(selected, str) and not selected.startswith('package://'):
        defaults_path = Path(selected).expanduser()
        if not defaults_path.is_absolute():
            defaults_path = Path(config_path).resolve().parent / defaults_path
    else:
        raise ValueError(f'unsupported inference_config {selected!r}')
    defaults = yaml.safe_load(defaults_path.read_text(encoding='utf-8')) or {}
    return dict(defaults, **configuration)


def registry_directory(config_path, smpa_root=None, problems=None):
    """Return the model registry folder SMPA's conversation server uses.

    SMPA uses ``models`` beside the configuration when it holds
    ``model_library.yaml``, otherwise the installed ``smpa_llm`` registry.
    """
    beside = Path(config_path).resolve().parent / 'models'
    if (beside / 'model_library.yaml').is_file():
        return beside
    folder = smpa_llm_folder(smpa_root, problems if problems is not None else [])
    return folder / 'models' if folder is not None else beside


def env_file_has(path, key):
    """Tell whether a ``.env`` file assigns a non-empty value to ``key``; never return the value."""
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip().removeprefix('export ').strip()
        if line.startswith(key + '=') and line.split('=', 1)[1].strip().strip('"\''):
            return True
    return False


def short(revision):
    """Shorten a commit hash for display."""
    return str(revision)[:10] if revision else None


def described(item, facts):
    """Put a configured description (if SMPA ever provides one) before the configuration facts."""
    text = item.get('description')
    return f'{text.strip()} ({facts.rstrip(".")})' if isinstance(text, str) and text.strip() else facts


def catalogue(config_path, model_root=None, environment=None, smpa_root=None):
    """Describe every conversational model choice in SMPA's configuration.

    Returns ``(models, problems)``. Each model has ``source`` (SMPA's own
    name for the choice, for example ``local:qwen3-4b`` or ``api:openai``),
    ``kind``, ``name``, ``provider``, ``backend``, ``model_id``,
    ``revision``, ``description``, ``default`` (the configured conversation
    agent) and a precheck ``availability``. Unreadable files are reported
    in ``problems`` instead of raising.
    """
    environment = os.environ if environment is None else environment
    config_path = Path(config_path)
    problems, models = [], []
    try:
        config = effective_configuration(config_path, smpa_root, problems)
    except (OSError, ValueError, yaml.YAMLError) as error:
        return [], problems + [f'SMPA configuration {config_path} cannot be read: {error}']
    conversation = config.get('conversation') if isinstance(config.get('conversation'), dict) else {}
    default = conversation.get('agent')
    modelling = conversation.get('modelling_agent')
    registry = registry_directory(config_path, smpa_root, problems)
    # SMPA reads credentials from .env beside the configuration, else from its working directory (the checkout).
    env_file = config_path.resolve().parent / '.env'
    if not env_file.is_file() and smpa_root:
        env_file = Path(smpa_root) / '.env'
    model_root = Path(model_root) if model_root else config_path.resolve().parent / 'models'
    try:
        entries = (yaml.safe_load((registry / 'model_library.yaml').read_text(encoding='utf-8')) or {}).get('models') or {}
    except (OSError, yaml.YAMLError, AttributeError) as error:
        entries = {}
        problems.append(f'Model registry {registry / "model_library.yaml"} cannot be read: {error}')
    for name, item in entries.items():
        if not isinstance(item, dict) or item.get('backend') not in ('transformers', 'ollama'):
            continue
        path = item.get('path')
        parts = [f'Local {item["backend"]} model {item.get("model_id", name)}']
        if item.get('revision'):
            parts.append(f'revision {short(item["revision"])}')
        if path:
            parts.append(f'stored at models/{path}')
        if item['backend'] == 'transformers':
            folder = model_root / path if path else None
            present = bool(folder and (folder / 'config.json').is_file())
            availability = dict(state='likely' if present else 'unavailable', verified=False,
                                detail='Weights folder found; SMPA verifies the weights when the conversation starts.'
                                if present else f'Weights not found at {folder}. Install the model first; there is no cloud fallback.')
        else:
            availability = dict(state='unknown', verified=False, detail='Served by Ollama; checked when the conversation starts.')
        models.append(dict(source=f'local:{name}', kind='local', name=name, provider=item['backend'],
                           backend=item['backend'], model_id=item.get('model_id', name), revision=item.get('revision'),
                           description=described(item, ', '.join(parts) + '.'), availability=availability))
    for name, item in config.items():
        if not (isinstance(item, dict) and isinstance(item.get('models'), dict) and item['models'].get('text')
                and item.get('base_url') and item.get('api_key_env')):
            continue
        key = item['api_key_env']
        configured = bool(str(environment.get(key, '')).strip()) or env_file_has(env_file, key)
        models.append(dict(source=f'api:{name}', kind='api', name=name, provider=name, backend='api',
                           model_id=item['models']['text'], revision=None,
                           description=described(item, f'API provider {name}, model {item["models"]["text"]}, '
                                                       f'endpoint {item["base_url"]}, credentials from {key}.'),
                           availability=dict(state='likely' if configured else 'unavailable', verified=False,
                                             detail=f'{key} is set; the provider is contacted on the first message.'
                                             if configured else f'Missing API credentials: set {key} in the environment or .env.')))
    for model in models:
        model['default'] = model['source'] == default
        model['modelling_agent'] = model['source'] == modelling
        model['agent_id'] = None
    if default and not any(model['default'] for model in models):
        problems.append(f'conversation.agent {default} is not among the configured models; SMPA will refuse to start.')
    return models, list(dict.fromkeys(problems))


def merge_server_agents(models, agents):
    """Replace prechecks with the conversation server's own verdicts and agent ids, matched by source."""
    by_source = {agent.get('source'): agent for agent in agents or []}
    merged = []
    for model in models:
        agent = by_source.get(model['source'])
        if agent:
            model = dict(model, agent_id=agent.get('id'), label=agent.get('label'),
                         availability=dict(state='available' if agent.get('available', True) else 'unavailable',
                                           verified=True, detail=agent.get('availability')))
        merged.append(model)
    return merged
