"""Sense service, camera capture, and observation persistence."""
from datetime import datetime
from importlib.resources import files
from pathlib import Path
import json
import signal
from time import perf_counter

from ament_index_python.packages import get_package_share_directory
from PIL import Image as PILImage
import rclpy
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import DescribeScene
import yaml

from smpa_ros.backends.base import (stage_generation, generation_arguments, validate_generation,
                                    generation_capabilities)

from smpa_ros.backends.transformers import TransformersVLM
from memory import MemoryManager
from smpa_ros.model import resolve_vision_model
from smpa_ros.plan import refinement_settings


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
        defaults = yaml.safe_load(
            (Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text(encoding='utf-8'))
        configuration = dict(defaults, **(configuration or {}))
    settings = stage_generation(configuration, 'sense')
    return prompt, settings


def record_observation(directory, cycle, sense, *, record_name='sense.json',
                       provenance=None, prompt_name=None, configuration=None):
    """Record every Sense invocation and identify new observation files."""
    destination = directory / 'state' / f'cycle_{cycle:06d}'
    destination.mkdir(exist_ok=True)
    prior_files = set((directory / 'observations').rglob('*'))
    record = dict(stage='sense', cycle=cycle, **(provenance or {}))
    try:
        if prompt_name is not None:
            record['prompt'], record['generation_parameters'] = load_prompt('', prompt=prompt_name, configuration=configuration)
        value = sense(directory, cycle)
        record.update(output=value, status='generated')
    except (Exception, KeyboardInterrupt) as error:
        record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=f'{type(error).__name__}: {error}')
        raise
    finally:
        with (destination / record_name).open('x', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
    observations = [str(path.relative_to(directory)) for path in sorted(
        set((directory / 'observations').rglob('*')) - prior_files) if path.is_file()]
    return value, observations


def observe(node, config, directory, executor, timeout, service, speech=None):
    """Collect the enabled vision and speech observations."""
    outputs = {}
    transcript = ''
    if service in ('vision', 'all'):
        outputs['vision'] = capture_once(node, config, directory, executor, timeout).read_text()
    if speech is not None:
        from smpa_ros_interfaces.action import Listen
        result = speech.perform(speech.listener, Listen.Goal(language='en', max_duration=10.0))
        transcript = result.transcription
        outputs['speech'] = result.transcription
    return json.dumps(outputs, ensure_ascii=False), transcript


def image_to_pil(image: Image) -> PILImage.Image:
    """Decode common 8-bit ROS encodings, including padded rows."""
    formats = {
        'rgb8': ('RGB', 'RGB', 3), 'bgr8': ('RGB', 'BGR', 3),
        'rgba8': ('RGBA', 'RGBA', 4), 'bgra8': ('RGBA', 'BGRA', 4),
        'mono8': ('L', 'L', 1),
    }
    if image.encoding not in formats:
        raise ValueError(f'Unsupported image encoding: {image.encoding}')
    mode, raw_mode, channels = formats[image.encoding]
    if (image.width == 0 or image.height == 0 or image.step < image.width * channels
            or len(image.data) != image.step * image.height):
        raise ValueError('Invalid image dimensions, stride, or data length')
    return PILImage.frombytes(
        mode, (image.width, image.height), bytes(image.data),
        'raw', raw_mode, image.step, 1,
    ).convert('RGB')


class SenseService(Node):
    def __init__(self, backend: TransformersVLM | None = None) -> None:
        super().__init__('sense')
        self.declare_parameter('world_model_dir', '')
        self.declare_parameter('run_id', '')
        self.world_model_dir, self.run_id = MemoryManager.assigned_run(
            self.get_parameter('world_model_dir').value, self.get_parameter('run_id').value)
        self.get_logger().info('Sense node starting')
        share = Path(get_package_share_directory('smpa_ros'))
        self.declare_parameter('config', str(share / 'config.yaml'))
        pipeline = yaml.safe_load(Path(self.get_parameter('config').value).read_text())
        self.refinement = refinement_settings(pipeline)
        self.image = None
        self.image_sha256 = None
        self.pipeline = pipeline
        vision = pipeline['model'] if isinstance(pipeline.get('model'), dict) else pipeline.get('vision', {})
        vision_backend = vision.get('backend', 'transformers')
        if vision_backend not in ('transformers', 'api'):
            raise ValueError('vision.backend must be transformers or api')
        self.declare_parameter('model_config', (pipeline.get('model', '') if isinstance(pipeline.get('model'), str) else vision.get('model', '')) + '.yaml'
                               if vision_backend == 'transformers' and not vision.get('path') else '')
        self.declare_parameter('device', vision.get('device', 'auto'))
        self.declare_parameter('dtype', vision.get('dtype', 'auto'))
        generation = stage_generation(pipeline, 'sense')
        provider = pipeline.get(vision.get('provider', 'openai'), {})
        capabilities = generation_capabilities(vision_backend, vision=True, provider=provider,
            model_id=vision.get('model_id', provider.get('models', {}).get('vision')))
        if vision_backend == 'api' and not provider.get('generation_capabilities'):
            capabilities = None
        validate_generation(generation, backend=vision_backend, capabilities=capabilities)
        token_limit = generation['max_output_tokens']
        if type(token_limit) is not int or token_limit <= 0:
            raise ValueError('vision.max_output_tokens must be a positive integer')
        self.declare_parameter('max_new_tokens', token_limit)
        self.max_new_tokens = self.get_parameter('max_new_tokens').value
        if type(self.max_new_tokens) is not int or self.max_new_tokens <= 0:
            raise ValueError('max_new_tokens must be a positive integer')
        self.generation_options = generation_arguments(dict(generation, max_output_tokens=self.max_new_tokens))
        self.default_prompt = files('smpa_ros').joinpath(
            'prompts/sense/describe_scene.txt',
        ).read_text(encoding='utf-8').strip()
        if backend is None and vision_backend == 'api':
            from smpa_ros.backends.api import APIBackend
            api_path = Path(self.get_parameter('config').value).resolve()
            backend = APIBackend(api_path, modality='vision', provider=vision.get('provider', 'openai'))
            backend.model_id = vision.get('model_id', backend.model_id)
            self.get_logger().info(f'Using API vision model: {backend.model_id}')
        elif backend is None:
            config = resolve_vision_model(pipeline, share / 'models', Path.cwd() / 'models',
                                          model_config=self.get_parameter('model_config').value or None)
            model_source = config['path']
            self.get_logger().info(
                f"Selected model: {config['model_id']}; backend: TransformersVLM; "
                f"device: {self.get_parameter('device').value}; dtype: {self.get_parameter('dtype').value}",
            )
            self.get_logger().info(f'Loading model weights from {model_source} (local files only)')
            started = perf_counter()
            try:
                backend = TransformersVLM(model_source, self.get_parameter('dtype').value,
                                          device=self.get_parameter('device').value)
            except Exception as error:
                self.get_logger().error(
                    f'Model loading failed: {error}. Check vision.device/vision.dtype and local weights; '
                    'run ./install.sh if model files are missing.')
                raise
            self.get_logger().info(f'Model loaded on {backend.model.device} with {backend.model.dtype} '
                                   f'in {perf_counter() - started:.2f}s')
        else:
            self.get_logger().info(f'Using supplied backend: {type(backend).__name__}')
        self.backend = backend
        self.service = self.create_service(DescribeScene, '/sense', self.describe)
        self.get_logger().info('/sense service ready')

    def describe(self, request: DescribeScene.Request,
                 response: DescribeScene.Response) -> DescribeScene.Response:
        self.get_logger().info('Sense request received')
        started = perf_counter()
        try:
            if self.image is None or not self.refinement['enabled']:
                self.image = image_to_pil(request.image)
                if self.refinement['enabled']:
                    import hashlib
                    self.image_sha256 = hashlib.sha256(self.image.tobytes()).hexdigest()
                    self.image.save(self.world_model_dir / 'observations/vision/frozen_image.png')
            image = self.image.copy()
            # Bound image size before processor tiling to limit inference memory.
            image.thumbnail((512, 512))
            response.description = self.backend.describe(
                image, request.prompt if request.prompt.strip() else self.default_prompt,
                **self.generation_options,
            )
            self.get_logger().info(f'Inference completed in {perf_counter() - started:.2f}s')
        except Exception as error:
            response.error = str(error)
            self.get_logger().error(
                f'Sense request failed after {perf_counter() - started:.2f}s: {error}',
            )
        return response

    def focus(self, target, question, *, settings=None):
        """Attend to a named entity/region in the cached image without another model."""
        if self.image is None:
            raise RuntimeError('Observe requires an initial Sense image')
        if not isinstance(question, str) or not question.strip():
            raise ValueError('Observe requires a nonempty question')
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise ValueError('Observe target must be a nonempty entity/region or null')
        prompt = self.pipeline.get('stage_prompts', {}).get('sense', {}).get('text', self.default_prompt)
        prompt += '\nFocus on: ' + (target or 'the entire image') + '\nQuestion: ' + question
        prompt += '\nUse only visible evidence. State when the answer cannot be determined visually.'
        if self.refinement['enabled']:
            prompt += ('\nReturn JSON with status answered when visible evidence answers the question, '
                       'visually_indeterminable when the property cannot be established from this image, '
                       'or unanswered when no conclusion was reached. Put the answer or explanation in answer.')
        generation = settings or stage_generation(self.pipeline, 'sense')
        image = self.image.copy()
        image.thumbnail((512, 512))
        result = self.backend.describe(image, prompt, **generation_arguments(generation))
        return result, prompt, generation


def capture_once(node: Node, config: dict, output_dir: Path,
                 executor: SingleThreadedExecutor, timeout: float = 120) -> Path:
    """Receive one frame, request its description, and write the exact response."""
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.task import Future
    from sensor_msgs.msg import Image
    from smpa_ros_interfaces.srv import DescribeScene

    output_dir, _ = MemoryManager.assigned_run(output_dir, config.get('run_id', ''))
    output_dir = output_dir / 'observations' / 'vision'
    selected = config.get('stage_prompts', {}).get('sense', {})
    prompt = (selected['text'] if config['prompt'] == selected.get('name') else
              files('smpa_ros').joinpath('prompts/sense/' + config['prompt'] + '.txt')
              .read_text(encoding='utf-8').strip())
    client = node.create_client(DescribeScene, '/sense')
    subscription = None
    try:
        node.get_logger().info('Waiting for /sense service')
        if not client.wait_for_service(timeout_sec=timeout):
            raise TimeoutError(f'/sense is unavailable after {timeout:g}s')
        image_future = Future()

        def receive(image: Image) -> None:
            if not image_future.done():
                image_future.set_result(image)

        node.get_logger().info(f"Subscribing to camera topic: {config['camera_topic']}")
        subscription = node.create_subscription(
            Image, config['camera_topic'], receive, qos_profile_sensor_data,
        )
        node.get_logger().info('Waiting for first image')
        started = perf_counter()
        executor.spin_until_future_complete(image_future, timeout_sec=timeout)
        if not image_future.done():
            raise TimeoutError(f"No image received on {config['camera_topic']} after {timeout:g}s")
        node.get_logger().info(f'Image received after {perf_counter() - started:.2f}s')
        node.destroy_subscription(subscription)
        subscription = None
        started = perf_counter()
        response_future = client.call_async(
            DescribeScene.Request(image=image_future.result(), prompt=prompt),
        )
        node.get_logger().info('Request sent to /sense')
        executor.spin_until_future_complete(response_future, timeout_sec=timeout)
        if not response_future.done():
            raise TimeoutError(f'/sense response timed out after {timeout:g}s')
        response = response_future.result()
        node.get_logger().info(f'/sense response received after {perf_counter() - started:.2f}s')
        if response.error:
            raise RuntimeError(response.error)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
        path = output_dir / f'{timestamp}_sense_output.md'
        # Exclusive creation prevents an existing observation from being overwritten.
        with path.open('x', encoding='utf-8', newline='') as output:
            output.write(response.description)
        node.get_logger().info(f'Markdown file saved: {path.resolve()}')
        return path
    except Exception as error:
        node.get_logger().error(f'Camera-to-Sense pipeline failed: {error}')
        raise
    finally:
        if subscription is not None:
            node.destroy_subscription(subscription)
        node.destroy_client(client)


def main(args=None):
    node = None
    try:
        rclpy.init(args=args)
        node = SenseService()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        rclpy.logging.get_logger('sense').info('Sense shutdown requested')
    finally:
        # Finish cleanup even if a further SIGINT arrives from the launch process group.
        previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                if node is not None:
                    node.destroy_node()
            finally:
                if rclpy.ok():
                    rclpy.shutdown()
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
