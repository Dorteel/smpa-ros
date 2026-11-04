"""Generate and persist world-model snapshots from Sense observations."""
from importlib.resources import files
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml


def load_backend(config, config_path, model_root, registry):
    """Return the shared lazy text generator and its model metadata."""
    registry_entries = yaml.safe_load((registry / 'model_library.yaml').read_text())['models']
    if config['modeling'].get('backend') == 'api':
        selected_name = 'api'
        provider = config['modeling'].get('provider', 'openai')
        settings = config[provider]
        selected = {'backend': 'api', 'provider': provider, 'model_id': settings['models']['text']}
    else:
        selected_name = config['modeling']['model']
        selected = registry_entries[selected_name]
        if config['modeling'].get('backend', selected['backend']) != selected['backend']:
            raise ValueError('modeling.backend must match the selected registry backend or be api')
    modeling_backend = None

    def model(prompt, *, max_new_tokens, schema=None, reasoning_effort=None):
        nonlocal modeling_backend
        generation_options = {}
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
            elif selected['backend'] == 'ollama':
                from smpa_ros.backends.ollama import OllamaBackend
                modeling_backend = OllamaBackend(selected['model_id'])
            else:
                raise ValueError(f"Unsupported modeling backend: {selected['backend']}")
        if schema is not None:
            if selected['backend'] not in ('transformers', 'api'):
                raise RuntimeError('Selected backend does not provide the required schema-constrained planning')
            return modeling_backend.generate(prompt, max_new_tokens=max_new_tokens, schema=schema, **generation_options)
        return modeling_backend.generate(prompt, max_new_tokens=max_new_tokens, **generation_options)

    return model, dict(selected, name=selected_name)


def load_prompt(value, previous_world_model='', *, prompt='default', configuration=None):
    component = files('smpa_ros')
    if not prompt or '/' in prompt or '\\' in prompt or prompt in ('.', '..'):
        raise ValueError('Prompt must be a filename stem')
    prompt = component.joinpath(f'prompts/model/{prompt}.txt').read_text(encoding='utf-8').format(
        input=value, previous_world_model=previous_world_model, goal='', available_actions='')
    if configuration is None or 'stage_settings' not in configuration:
        from ament_index_python.packages import get_package_share_directory
        configuration = yaml.safe_load(
            (Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text(encoding='utf-8'))
    settings = dict(configuration['stage_settings']['model'])
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
        if 'max_output_tokens' in pipeline.get('modeling', {}):
            settings['max_new_tokens'] = pipeline['modeling']['max_output_tokens']
        if pipeline.get('modeling', {}).get('reasoning_effort') is not None:
            settings['reasoning_effort'] = pipeline['modeling']['reasoning_effort']
        record['prompt'] = prompt
        if model is None:
            raise ValueError('Model stage requires a configured local LLM backend')
        generate = getattr(model, 'generate', model)
        world_model = generate(prompt, **settings)
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
