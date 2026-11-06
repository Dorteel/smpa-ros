"""Generate and persist world-model snapshots from Sense observations."""
from importlib.resources import files
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from smpa_ros.backends.base import (GENERATION_KEYS, stage_generation, generation_arguments, validate_generation,
                                    generation_capabilities, validate_output)


def unify_model(config):
    """Use the existing Sense selection as the global selection in legacy configs."""
    from copy import deepcopy
    result = deepcopy(config)
    # Resolve historical generation overrides before removing stage model selections.
    effective = {stage: stage_generation(config, stage) for stage in ('sense', 'model', 'plan', 'act')}
    selected = config.get('model')
    if not isinstance(selected, dict):
        selected = dict(config.get('vision', {}), model=selected)
    else:
        selected = dict(selected)
    selected.setdefault('backend', 'transformers')
    if selected['backend'] == 'api':
        selected.setdefault('provider', 'openai')
        selected.setdefault('model_id', config[selected['provider']]['models']['vision'])
    for key in (*GENERATION_KEYS, 'output_schema'):
        selected.pop(key, None)
    result['model'] = selected
    result['stage_settings'] = effective
    for section in ('vision', 'modeling', 'planning'):
        result.pop(section, None)
    return result


def local_model_info(directory, *, vision=False, model_id=None, model_root=None):
    """Inspect local inference files without loading weights or querying the Hub."""
    import importlib.util
    directory = Path(directory).expanduser().resolve()
    root = Path(model_root).resolve() if model_root is not None else directory.parent.parent
    result = dict(path=str(directory), model_id=model_id or str(directory),
                  status='unavailable', reason='Directory does not exist')
    if not directory.is_dir():
        return result
    result.update(status='incomplete', reason='')
    try:
        config = json.loads((directory / 'config.json').read_text())
        report = root / 'download_report.json'
        records = json.loads(report.read_text()).get('models', []) if report.is_file() else []
        record = next((entry for entry in records if
            Path(entry.get('local_dir', '')).resolve() == directory or
            model_id is not None and entry.get('repo_id') == model_id), {})
        result['model_id'] = model_id or record.get('repo_id') or config.get('_name_or_path') or str(directory)
        from transformers.models.auto.modeling_auto import (MODEL_FOR_CAUSAL_LM_MAPPING_NAMES,
                                                            MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES)
        mapping = MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES if vision else MODEL_FOR_CAUSAL_LM_MAPPING_NAMES
        if config.get('model_type') not in mapping:
            result.update(status='unavailable', reason='Model type is unsupported for this stage')
            return result
        if not any((directory / name).is_file() for name in ('tokenizer.json', 'tokenizer.model')):
            raise ValueError('Missing tokenizer.json or tokenizer.model')
        if vision and not any((directory / name).is_file() for name in ('processor_config.json', 'preprocessor_config.json')):
            raise ValueError('Missing processor configuration')
        indexes = [directory / name for name in ('model.safetensors.index.json', 'pytorch_model.bin.index.json')
                   if (directory / name).is_file()]
        weights = set()
        for index in indexes:
            weights.update(json.loads(index.read_text())['weight_map'].values())
        if not indexes:
            weights = {name for name in ('model.safetensors', 'pytorch_model.bin') if (directory / name).is_file()}
        if not weights:
            raise ValueError('Missing model weights or shard index')
        for name in weights:
            weight = (directory / name).resolve()
            if not weight.is_relative_to(directory) or not weight.is_file() or weight.stat().st_size == 0:
                raise ValueError(f'Missing or empty weight shard: {name}')
        if record.get('files'):
            # Reuse the downloader's manifest verifier. Hashing multi-GB weights is
            # reserved for downloader verification, not each GUI refresh.
            script = root.parent / 'scripts' / 'download_models.py'
            if not script.is_file():
                script = Path.cwd() / 'scripts' / 'download_models.py'
            if not script.is_file():
                raise ValueError('Downloader verification script is unavailable')
            spec = importlib.util.spec_from_file_location('_smpa_download_verification', script)
            downloader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(downloader)
            manifest = {name: dict(size=details['size']) for name, details in record['files'].items()}
            if not downloader.verified(directory, manifest):
                raise ValueError('Downloader manifest contains missing or wrong-sized files')
        result.update(status='complete', reason='Required files and available manifest sizes checked')
    except (OSError, ValueError, KeyError, TypeError) as error:
        result['reason'] = str(error)
    except ImportError:
        result.update(status='unavailable', reason='Transformers inference dependencies are unavailable')
    return result


def resolve_vision_model(config, registry, model_root, *, model_config=None):
    """Resolve explicit paths or legacy YAML/report selections; never use Hub IDs as paths."""
    vision = dict(config['model'] if isinstance(config.get('model'), dict) else config.get('vision', {}))
    frozen = config.get('_run', {}).get('stage_models', {}).get('sense', {})
    if frozen.get('path'):
        vision.update(path=frozen['path'], model_id=frozen['model_id'])
    root = Path(model_root).resolve()
    if vision.get('path'):
        directory = Path(vision['path']).expanduser()
        if not directory.is_absolute():
            directory = (root.parent if directory.parts[0] == root.name else root) / directory
        selected = dict(vision, name=vision.get('model', directory.name))
    else:
        name = vision.get('model') if isinstance(config.get('model'), dict) else config.get('model')
        if not model_config and not isinstance(name, str):
            raise ValueError('Select a local model directory or registered vision model')
        definition = Path(model_config or (name + '.yaml'))
        if not definition.is_absolute():
            definition = Path(registry) / definition
        selected = dict(yaml.safe_load(definition.read_text()), **vision)
        directory = selected.get('path')
        if directory:
            directory = Path(directory)
            if not directory.is_absolute():
                directory = root / directory
        else:
            report = root / 'download_report.json'
            records = json.loads(report.read_text()).get('models', []) if report.is_file() else []
            record = next((entry for entry in records if entry.get('repo_id') == selected['model_id']), {})
            directory = record.get('local_dir')
            if not directory:
                raise ValueError(f"Local model {selected['model_id']} is unavailable: no local path in the download report")
    info = local_model_info(directory, vision=True, model_id=selected.get('model_id'), model_root=root)
    if info['status'] != 'complete':
        raise ValueError(f"Local model {info['path']} is {info['status']}: {info['reason']}")
    return dict(selected, backend='transformers', model_id=info['model_id'], path=info['path'])


def load_backend(config, config_path, model_root, registry):
    """Return the shared lazy text generator and its model metadata."""
    if config['modeling'].get('backend') == 'api':
        selected_name = 'api'
        provider = config['modeling'].get('provider', 'openai')
        settings = config[provider]
        selected = {'backend': 'api', 'provider': provider, 'model_id': config['modeling'].get('model_id', settings['models']['text'])}
    elif config['modeling'].get('path') and config['modeling'].get('backend', 'transformers') == 'transformers':
        selected = dict(config['modeling'])
        selected.setdefault('backend', 'transformers')
        selected_name = selected.get('model', Path(selected['path']).name)
    else:
        registry_entries = yaml.safe_load((registry / 'model_library.yaml').read_text())['models']
        selected_name = config['modeling']['model']
        selected = dict(registry_entries[selected_name])
        if config['modeling'].get('backend', selected['backend']) != selected['backend']:
            raise ValueError('modeling.backend must match the selected registry backend or be api')
    if selected['backend'] == 'transformers':
        path = Path(selected['path']).expanduser()
        path = path if path.is_absolute() else Path(model_root) / path
        info = local_model_info(path, model_id=selected.get('model_id'), model_root=model_root)
        if info['status'] != 'complete':
            raise ValueError(f"Local model {info['path']} is {info['status']}: {info['reason']}")
        selected.update(path=info['path'], model_id=info['model_id'])
    modeling_backend = None

    def model(prompt, *, max_new_tokens, schema=None, reasoning_effort=None, **options):
        nonlocal modeling_backend
        generation_options = dict(options)
        response = options.get('response_format') or ('json_schema' if schema is not None else 'text')
        capabilities = generation_capabilities(selected['backend'],
            provider=config.get(selected.get('provider'), {}), model_id=selected['model_id'])
        if selected['backend'] == 'api' and not config.get(selected.get('provider'), {}).get('generation_capabilities'):
            capabilities = None
        validate_generation(dict(max_output_tokens=max_new_tokens, output_schema=schema,
            response_format=response, reasoning_effort=reasoning_effort,
            temperature=options.get('temperature'), seed=options.get('seed')),
            backend=selected['backend'], capabilities=capabilities)
        if reasoning_effort is not None:
            if selected['backend'] != 'api':
                raise ValueError('reasoning_effort is supported only for API inference')
            generation_options['reasoning_effort'] = reasoning_effort
        if modeling_backend is None:
            if selected['backend'] == 'transformers':
                from smpa_ros.backends.transformers import TransformersBackend
                path = Path(model_root) / selected['path']
                modeling_backend = TransformersBackend(str(path), enable_thinking=selected.get('enable_thinking'))
            elif selected['backend'] == 'api':
                from smpa_ros.backends.api import APIBackend
                modeling_backend = APIBackend(config_path, modality='text', provider=selected['provider'])
                modeling_backend.model_id = selected['model_id']
            elif selected['backend'] == 'ollama':
                from smpa_ros.backends.ollama import OllamaBackend
                modeling_backend = OllamaBackend(selected['model_id'])
            else:
                raise ValueError(f"Unsupported modeling backend: {selected['backend']}")
        if schema is not None:
            if selected['backend'] not in ('transformers', 'api', 'ollama'):
                raise RuntimeError('Selected backend does not provide the required schema-constrained planning')
            return modeling_backend.generate(prompt, max_new_tokens=max_new_tokens, schema=schema, **generation_options)
        return modeling_backend.generate(prompt, max_new_tokens=max_new_tokens, **generation_options)

    return model, dict(selected, name=selected_name)


def load_prompt(value, previous_world_model='', *, prompt='default', configuration=None):
    component = files('smpa_ros')
    if not prompt or '/' in prompt or '\\' in prompt or prompt in ('.', '..'):
        raise ValueError('Prompt must be a filename stem')
    selected = (configuration or {}).get('stage_prompts', {}).get('model', {})
    template = (selected['text'] if prompt == selected.get('name') else
                component.joinpath(f'prompts/model/{prompt}.txt').read_text(encoding='utf-8'))
    prompt = template.format(
        input=value, previous_world_model=previous_world_model, goal='', available_actions='')
    if configuration is None:
        from ament_index_python.packages import get_package_share_directory
        defaults = yaml.safe_load(
            (Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text(encoding='utf-8'))
        configuration = dict(defaults, **(configuration or {}))
    settings = stage_generation(configuration, 'model')
    return prompt, settings


def validate_settings(settings):
    effort = settings.get('reasoning_effort')
    if effort is not None and effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
        raise ValueError('modeling.reasoning_effort is unsupported')
    if 'max_output_tokens' in settings:
        limit = settings['max_output_tokens']
        if type(limit) is not int or limit <= 0:
            raise ValueError('modeling.max_output_tokens must be a positive integer')


def generate_world_model(directory, cycle, value, model, *, observations, pipeline,
                         models=None, model_identifier=None, prompt_name='default',
                         record_name='model.json', previous_snapshot=None, provenance=None):
    """Generate one snapshot and record its provenance, preserving earlier iterations."""
    destination = directory / 'state' / f'cycle_{cycle:06d}'
    source_record = str((destination / 'sense.json').relative_to(directory))
    previous = directory / 'world_model' / f'iteration_{cycle - 1:03d}.md' if cycle > 1 else None
    if previous_snapshot is not None:
        previous = directory / previous_snapshot if previous_snapshot else None
    previous_text = previous.read_text(encoding='utf-8') if previous is not None else ''
    snapshot = directory / 'world_model' / f"iteration_{cycle:03d}{record_name[len('model'):-len('.json')]}.md"
    backend_id = getattr(model, 'model_id', None) or getattr(model, 'model', None)
    record = dict(stage='model', cycle=cycle, input=value, prompt=None,
                  source_observations=([provenance['source_record']] if provenance and provenance.get('source_record')
                                       else [] if provenance is not None else [source_record]) + observations,
                  previous_iteration=str(previous.relative_to(directory)) if previous else None,
                  iteration=str(snapshot.relative_to(directory)),
                  model_identifier=model_identifier or (models or {}).get('model', {}).get('model_id')
                  or (backend_id if isinstance(backend_id, str) else type(model).__name__))
    record.update(provenance or {})
    try:
        prompt, settings = load_prompt(value, previous_text, prompt=prompt_name, configuration=pipeline)
        record['generation_parameters'] = settings
        record['prompt'] = prompt
        if model is None:
            raise ValueError('Model stage requires a configured local LLM backend')
        generate = getattr(model, 'generate', model)
        world_model = generate(prompt, **generation_arguments(settings))
        validate_output(world_model, settings['response_format'], settings.get('output_schema') if settings['response_format'] == 'json_schema' else None)
        if not isinstance(world_model, str) or not world_model.strip():
            raise ValueError('Model returned an empty world model')
        # Publish a complete snapshot exclusively; never replace an earlier iteration.
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=snapshot.parent,
                                prefix='.iteration-', delete=False) as output:
            pending = Path(output.name)
            try:
                output.write(world_model)
                output.flush()
                os.fsync(output.fileno())
                os.link(pending, snapshot)
            finally:
                pending.unlink(missing_ok=True)
        record.update(status='generated', output=world_model)
    except (Exception, KeyboardInterrupt) as error:
        record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=f'{type(error).__name__}: {error}', iteration=None)
        with (destination / record_name).open('x', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
        raise
    with (destination / record_name).open('x', encoding='utf-8') as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
    return world_model
