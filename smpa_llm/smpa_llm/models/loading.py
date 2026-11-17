"""Select and lazily load inference models without cognitive stage ownership."""
import json
from pathlib import Path
import yaml
from smpa_llm.inference.backends.base import generation_capabilities, validate_generation

def local_model_info(directory, *, vision=False, model_id=None, model_root=None):
    """Inspect local inference assets without loading weights or querying the Hub.

    Resolve the selected directory and optional storage-root manifest. Check
    supported architecture, tokenizer, processor when needed, and all weight
    shards. Reuse size-only manifest validation when a report is available.
    Return identity, status and a diagnostic reason; missing dependencies or
    incomplete assets are reported without repairing or downloading files.
    """
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
            from smpa_llm.models import provision as downloader
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
    """Resolve a local vision selection while preserving legacy YAML and report paths.

    Prefer a frozen run path, then an explicit configured path. Otherwise read
    the selected model definition and resolve its local destination or download
    report. Interpret relative paths against the supplied storage root, never
    a Hub identifier. Validate local assets and return the selected identity
    and absolute path, or raise ValueError when the model is unavailable.
    """
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

def load_backend(config, config_path, model_root, registry, *, conversation=False):
    """Resolve a model and return the existing lazy inference callable.

    Robot callers keep local-file checks, generation validation and string
    results. Conversational discovery keeps registry paths unchanged so saved
    agent identities remain resumable and unavailable models can be shown in the
    GUI. Its first generation loads that model and opts into structured results
    and conversational controls. Neither path loads another workflow's model.
    """
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
    if selected['backend'] == 'transformers' and not conversation:
        path = Path(selected['path']).expanduser()
        path = path if path.is_absolute() else Path(model_root) / path
        info = local_model_info(path, model_id=selected.get('model_id'), model_root=model_root)
        if info['status'] != 'complete':
            raise ValueError(f"Local model {info['path']} is {info['status']}: {info['reason']}")
        selected.update(path=info['path'], model_id=info['model_id'])
    modeling_backend = None

    def model(prompt, *, max_new_tokens, schema=None, reasoning_effort=None, **options):
        """Generate with the selected workflow's existing output contract.

        Validate robot controls before loading weights. Conversational controls
        are validated by their backend's opt-in interface instead. Lazily reuse
        one backend, forwarding schemas without weakening their validation, and
        return a string for robot inference or a structured conversational result.
        """
        nonlocal modeling_backend
        generation_options = dict(options)
        if not conversation:
            if set(options) - {'temperature', 'seed', 'response_format'}:
                raise ValueError('Interactive generation settings are supported only in conversations')
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
                from smpa_llm.inference.backends.transformers import TransformersBackend
                path = Path(model_root) / selected['path']
                modeling_backend = TransformersBackend(str(path), enable_thinking=selected.get('enable_thinking'))
            elif selected['backend'] == 'api':
                from smpa_llm.inference.backends.api import APIBackend
                modeling_backend = APIBackend(config_path, modality='text', provider=selected['provider'])
                modeling_backend.model_id = selected['model_id']
            elif selected['backend'] == 'ollama':
                from smpa_llm.inference.backends.ollama import OllamaBackend
                modeling_backend = OllamaBackend(selected['model_id'])
            else:
                raise ValueError(f"Unsupported modeling backend: {selected['backend']}")
        if schema is not None:
            if selected['backend'] not in ('transformers', 'api', 'ollama'):
                raise RuntimeError('Selected backend does not provide the required schema-constrained planning')
            generate = modeling_backend.generate_conversation if conversation else modeling_backend.generate
            return generate(prompt, max_new_tokens=max_new_tokens, schema=schema, **generation_options)
        generate = modeling_backend.generate_conversation if conversation else modeling_backend.generate
        return generate(prompt, max_new_tokens=max_new_tokens, **generation_options)

    return model, dict(selected, name=selected_name)
