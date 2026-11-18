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


from smpa_llm.models.loading import local_model_info, resolve_vision_model, load_backend


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
    """Generate and persist one world-model snapshot and its invocation record.

    The assigned directory and cycle identify this invocation; the supplied
    observation, pipeline, model and provenance determine its prompt and source
    records. Hold a world-model-specific advisory lock while reading the previous
    snapshot, running the supplied inference callable, and exclusively publishing
    the next snapshot and record. This serializes writers to the same run without
    blocking unrelated runs or non-model operations.

    Return the generated world model after it has been validated and persisted.
    If generation fails, retain earlier snapshots, write the failure record, and
    re-raise the error. The caller remains responsible for unique record names.
    """
    import fcntl

    with (directory / '.world_model.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
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
