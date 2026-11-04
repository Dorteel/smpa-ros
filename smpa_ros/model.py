"""Generate and persist world-model snapshots from Sense observations."""
from importlib.resources import files
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def load_prompt(value, previous_world_model=''):
    component = files('smpa_ros')
    prompt = component.joinpath('prompts/model/default.txt').read_text(encoding='utf-8').format(
        input=value, previous_world_model=previous_world_model, goal='', available_actions='')
    settings = json.loads(component.joinpath('models/model/default.json').read_text(encoding='utf-8'))
    return prompt, settings


def generate_world_model(directory, cycle, value, model, *, observations, pipeline,
                         models=None, model_identifier=None):
    """Generate one snapshot and record its provenance, preserving earlier iterations."""
    destination = directory / 'state' / f'cycle_{cycle:06d}'
    source_record = str((destination / 'sense.json').relative_to(directory))
    previous = directory / 'world_model' / f'iteration_{cycle - 1:03d}.md' if cycle > 1 else None
    previous_text = previous.read_text(encoding='utf-8') if previous is not None else ''
    prompt, settings = load_prompt(value, previous_text)
    if 'max_output_tokens' in pipeline.get('modeling', {}):
        settings['max_new_tokens'] = pipeline['modeling']['max_output_tokens']
    if pipeline.get('modeling', {}).get('reasoning_effort') is not None:
        settings['reasoning_effort'] = pipeline['modeling']['reasoning_effort']
    snapshot = directory / 'world_model' / f'iteration_{cycle:03d}.md'
    backend_id = getattr(model, 'model_id', None) or getattr(model, 'model', None)
    record = dict(stage='model', cycle=cycle, input=value, prompt=prompt,
                  source_observations=[source_record, *observations],
                  previous_iteration=str(previous.relative_to(directory)) if previous else None,
                  iteration=str(snapshot.relative_to(directory)),
                  model_identifier=model_identifier or (models or {}).get('model', {}).get('model_id')
                  or (backend_id if isinstance(backend_id, str) else type(model).__name__))
    try:
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
        with (destination / 'model.json').open('x', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
        raise
    with (destination / 'model.json').open('x', encoding='utf-8') as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
    return world_model
