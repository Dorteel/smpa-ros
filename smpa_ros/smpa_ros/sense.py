"""Sense service, camera capture, and observation persistence."""
from importlib.resources import files
from pathlib import Path
import json
import yaml

from smpa_ros.backends.base import stage_generation


def load_prompt(value, *, prompt='default', configuration=None):
    component = files('smpa_ros')
    if not prompt or '/' in prompt or '\\' in prompt or prompt in ('.', '..'):
        raise ValueError('Prompt must be a filename stem')
    selected = (configuration or {}).get('stage_prompts', {}).get('sense', {})
    template = (selected['text'] if prompt == selected.get('name') else
                component.joinpath(f'prompts/sense/{prompt}.txt').read_text(encoding='utf-8'))
    prompt = (template if prompt == selected.get('name') else template.format(
        input=value, previous_world_model='', goal='', available_actions=''))
    if configuration is None:
        from ament_index_python.packages import get_package_share_directory
        from smpa_llm.configuration import load_configuration
        defaults = load_configuration(Path(get_package_share_directory('smpa_ros')) / 'config.yaml')
        configuration = dict(defaults, **(configuration or {}))
    settings = stage_generation(configuration, 'sense')
    return prompt, settings


def record_observation(directory, cycle, sense, *, record_name='sense.json',
                       provenance=None, prompt_name=None, configuration=None):
    """Persist one Sense invocation and its proposed Read cursor as one commit.

    Call the existing observation callback and identify all newly created sensory
    evidence, including evidence retained after failure. A successful attributed
    Read result supplies its proposed cursor; failed observations never do.
    Write the complete stage JSON to a temporary file, flush it durably, and link
    it exclusively to its final name. Recovery therefore sees either no record
    or one complete record containing both observation and cursor. Never overwrite
    an invocation, swallow the observation error, or consume messages separately.
    """
    import os
    from tempfile import NamedTemporaryFile
    destination = directory / 'state' / f'cycle_{cycle:06d}'
    destination.mkdir(exist_ok=True)
    prior_files = set((directory / 'observations').rglob('*'))
    record = dict(stage='sense', cycle=cycle, **(provenance or {}))
    try:
        if prompt_name is not None:
            record['prompt'], record['generation_parameters'] = load_prompt('', prompt=prompt_name, configuration=configuration)
        value = sense(directory, cycle)
        record.update(output=value, status='generated')
        from sense import modalities
        if modalities(configuration or {})['read']:
            text = json.loads(value).get('read', {})
            if 'cursor' in text:
                record['read_cursor'] = text['cursor']
    except (Exception, KeyboardInterrupt) as error:
        record.pop('read_cursor', None)
        record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=f'{type(error).__name__}: {error}')
        raise
    finally:
        observations = [str(path.relative_to(directory)) for path in sorted(
            set((directory / 'observations').rglob('*')) - prior_files) if path.is_file()]
        record['source_observations'] = observations
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination, prefix='.sense-', delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(record, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
                os.link(temporary, destination / record_name)
                descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)
    return value, observations


def observe(node, config, directory, executor, timeout, service, speech=None):
    """Collect enabled modalities sequentially with independent source timestamps.

    Preserve legacy vision/speech output keys and append attributed Read results
    only when explicitly enabled. Vision and Hear own their existing evidence;
    Read only observes the selected conversation and proposes a consumption
    cursor. The stage record commits that cursor after every modality succeeds.
    No conversational inference or RDF operation is invoked here.
    """
    from sense import modalities
    from sense.hear import observe as hear
    from sense.read import observe as read
    enabled = modalities(config, service)
    outputs, transcript = {}, ''
    if enabled['vision']:
        outputs['vision'] = capture_once(node, config, directory, executor, timeout).read_text()
    if enabled['hear']:
        if speech is None:
            raise RuntimeError('Hear requires its assigned Listen action client')
        transcript = hear(speech)
        outputs['speech'] = transcript
    if enabled['read']:
        outputs['read'] = read(directory, config['sensing']['read']['session'])
    return json.dumps(outputs, ensure_ascii=False), transcript

# Preserve established Python and console entry points during the module move.
from sense.vision import SenseService, image_to_pil, capture_once, main
