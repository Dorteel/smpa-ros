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


def observe(node, config, directory, executor, timeout, service, speech=None, *, capture=None):
    """Collect enabled modalities sequentially with independent source timestamps.

    Preserve legacy vision/speech output keys and append attributed Read results
    only when explicitly enabled. Vision and Hear own their existing evidence;
    Read only observes the selected conversation and proposes a consumption
    cursor. The stage record commits that cursor after every modality succeeds.
    An optional capture callable sends vision requests through an independent
    inference server; the legacy capture is used when none is supplied. No
    conversational inference or RDF operation is invoked here.
    """
    from sense import modalities
    from sense.hear import observe as hear
    from sense.read import observe as read
    enabled = modalities(config, service)
    outputs, transcript = {}, ''
    if enabled['vision']:
        outputs['vision'] = (capture or capture_once)(node, config, directory, executor, timeout).read_text()
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


def describe_image(node, image, prompt, *, run_id, directory, pipeline):
    """Describe camera pixels with the run-owned shared inference server.

    Check the active experiment and generation controls against the frozen
    Sense configuration. Preserve the original image for refinement and send
    a bounded RGB copy with the request-specific schema when configured.
    Return only successful inference output. Never load a model locally.
    """
    from sensor_msgs.msg import Image
    from action_msgs.msg import GoalStatus
    from smpa_llm.inference.images import image_to_pil
    from smpa_ros.plan import refinement_settings
    from smpa_ros_interfaces.action import DescribeImage, DescribeStructuredImage

    timeout = node.get_parameter('timeout').value
    settings = stage_generation(pipeline, 'sense')
    if settings.get('reasoning_effort') is not None:
        raise ValueError('The shared inference server does not expose reasoning_effort')
    if not node.parameters.wait_for_services(timeout_sec=timeout):
        raise TimeoutError('Inference parameter services are unavailable')
    response = node.result(node.parameters.get_parameters(['experiment_run_id', 'active_model']), timeout, 'inference ownership')
    if response.values[0].string_value != run_id or not response.values[1].string_value:
        raise RuntimeError('Inference server is not frozen for this experiment')
    alias = response.values[1].string_value
    names = [f'models.{alias}.{field}' for field in ('max_new_tokens', 'temperature', 'seed', 'top_p')]
    response = node.result(node.parameters.get_parameters(names), timeout, 'inference settings')
    actual = (response.values[0].integer_value, response.values[1].double_value,
              response.values[2].integer_value, response.values[3].double_value)
    expected = (settings.get('max_output_tokens'), settings.get('temperature'),
                settings.get('seed') if settings.get('seed') is not None else -1, 1.0)
    if actual != expected:
        raise ValueError(f'Inference parameters do not match frozen Sense settings: expected {expected}, got {actual}')
    pixels = image_to_pil(image)
    if refinement_settings(pipeline)['enabled']:
        frozen = directory / 'observations/vision/frozen_image.png'
        if frozen.exists():
            from PIL import Image as PILImage
            with PILImage.open(frozen) as stored:
                pixels = stored.convert('RGB')
        else:
            import os
            from tempfile import NamedTemporaryFile
            with NamedTemporaryFile(mode='wb', dir=frozen.parent, prefix='.frozen-image-', delete=False) as output:
                pending = Path(output.name)
                try:
                    pixels.save(output, format='PNG')
                    output.flush()
                    os.fsync(output.fileno())
                    try:
                        os.link(pending, frozen)
                    except FileExistsError:
                        from PIL import Image as PILImage
                        with PILImage.open(frozen) as stored:
                            pixels = stored.convert('RGB')
                finally:
                    pending.unlink(missing_ok=True)
    pixels.thumbnail((512, 512))
    message = Image(height=pixels.height, width=pixels.width, encoding='rgb8',
                    is_bigendian=0, step=pixels.width * 3, data=list(pixels.tobytes()))
    format_name = settings.get('response_format')
    if format_name == 'text':
        client = node.image_client
        goal = DescribeImage.Goal(image=message, prompt=prompt, run_id=run_id)
    elif format_name == 'json_schema' and isinstance(settings.get('output_schema'), dict):
        client = node.structured_client
        goal = DescribeStructuredImage.Goal(image=message, prompt=prompt,
                                              json_schema=json.dumps(settings['output_schema']), run_id=run_id)
    else:
        raise ValueError('Unsupported Sense response format')
    if not client.wait_for_server(timeout_sec=timeout):
        raise TimeoutError('Vision inference action is unavailable')
    handle = node.result(client.send_goal_async(goal), timeout, 'vision acceptance')
    if not handle.accepted:
        raise RuntimeError('Vision inference goal was rejected')
    response = node.result(handle.get_result_async(), timeout, 'vision result')
    if response.status != GoalStatus.STATUS_SUCCEEDED:
        raise RuntimeError('Vision inference action failed')
    return response.result.description if format_name == 'text' else response.result.json_output
