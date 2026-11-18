"""Vision service, camera capture, and observation persistence."""
from datetime import datetime, timezone
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
from memory import MemoryManager, append_observation
from smpa_ros.model import resolve_vision_model
from smpa_ros.plan import refinement_settings


from smpa_llm.inference.images import image_to_pil


def camera_provenance(image, topic, received_at):
    """Describe the original ROS frame and the time this process received it.

    Preserve the publisher's header stamp independently of the UTC receipt
    time. Return the same source fields used by normal camera PNGs so frozen
    refinement evidence carries equally explicit provenance.
    """
    return dict(topic=topic, received_at=received_at.isoformat(),
                stamp=dict(sec=image.header.stamp.sec, nanosec=image.header.stamp.nanosec),
                frame_id=image.header.frame_id, encoding=image.encoding,
                width=image.width, height=image.height, step=image.step,
                is_bigendian=image.is_bigendian)


def freeze_image(directory, pixels, camera, *, cycle=None, sense_record=None):
    """Durably freeze the first refinement frame with its camera provenance.

    Preserve an existing frozen image byte for byte. Otherwise save original
    pixels with a smpa.camera chunk containing the source metadata and frozen_at
    timestamp, fsync the temporary PNG, and publish it exclusively. Index only
    the process that wins publication, then remove its temporary file even if
    indexing fails. No capture timestamp is invented for older existing images.
    """
    import os
    from tempfile import NamedTemporaryFile
    from PIL.PngImagePlugin import PngInfo

    destination = directory / 'observations/vision/frozen_image.png'
    if destination.exists():
        return
    frozen = dict(camera, frozen_at=datetime.now(timezone.utc).isoformat())
    metadata = PngInfo()
    metadata.add_itxt('smpa.camera', json.dumps(frozen))
    with NamedTemporaryFile(mode='wb', dir=destination.parent, prefix='.frozen-image-', delete=False) as output:
        pending = Path(output.name)
        try:
            pixels.save(output, format='PNG', pnginfo=metadata)
            output.flush()
            os.fsync(output.fileno())
            try:
                os.link(pending, destination)
            except FileExistsError:
                return
            append_observation(directory, modality='vision', kind='camera_frame',
                observed_at=frozen['received_at'], status='evidence_without_description',
                files={'image': str(destination.relative_to(directory))}, cycle=cycle,
                sense_record=sense_record, provenance=frozen)
        finally:
            pending.unlink(missing_ok=True)


class SenseService(Node):
    def __init__(self, backend: TransformersVLM | None = None) -> None:
        """Bind an assigned visual run and expose its existing description service.

        Read the frozen configuration and generation controls. Reuse an explicitly
        supplied backend; only a standalone server loads its own configured
        local or API model. Keep original pixels for focused observation and
        expose the remappable DescribeScene endpoint without starting a loop.
        """
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
        """Describe the requested frame using the shared configured backend.

        Decode and cache original pixels, freezing initial evidence only when
        refinement is enabled. Reuse previously frozen pixels on a restarted
        service rather than pairing a new frame with old persisted evidence.
        Record source and freeze times for new evidence.
        Bound a copy to 512 pixels before inference and
        return exact generated text. Report failures in the existing error field;
        original evidence persistence belongs to the acquiring caller.
        """
        self.get_logger().info('Sense request received')
        started = perf_counter()
        try:
            if self.image is None or not self.refinement['enabled']:
                self.image = image_to_pil(request.image)
                if self.refinement['enabled']:
                    import hashlib
                    freeze_image(self.world_model_dir, self.image, camera_provenance(
                        request.image, self.pipeline.get('camera_topic', ''), datetime.now(timezone.utc)))
                    with PILImage.open(self.world_model_dir / 'observations/vision/frozen_image.png') as stored:
                        self.image = stored.convert('RGB')
                    self.image_sha256 = hashlib.sha256(self.image.tobytes()).hexdigest()
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
        """Answer a focused question using cached pixels and the same backend.

        Require an initial image and nonempty question, preserving optional target
        attribution in the prompt. Reuse configured structured generation, with
        refinement status controls when enabled. Return response, prompt and
        settings without acquiring another frame or loading another model.
        """
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
                 executor: SingleThreadedExecutor, timeout: float = 120, *, describe=None) -> Path:
    """Capture one image and description inside the assigned run's observations.

    Receive the first camera frame and its UTC receipt time, then unsubscribe
    and save the original full-resolution RGB pixels before requesting Sense.
    Embed source header and UTC receipt metadata in that PNG. Append its index
    entry before inference so a failed request still leaves discoverable camera
    evidence. Freeze the same frame when refinement is enabled. On success save
    and fsync the description, then append its separate manifest with any known
    cycle and Sense invocation supplied by the stage. Standalone captures retain
    null invocation fields rather than guessing an experiment association.
    Keep the image on inference error without fabricating a description. Create files exclusively; the
    existing caller records them as sources of one Sense invocation. An optional
    description callable uses the shared inference server instead of the legacy
    service. Always destroy the subscription and any service client before returning.
    """
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.task import Future
    from sensor_msgs.msg import Image
    from smpa_ros_interfaces.srv import DescribeScene

    run_directory, _ = MemoryManager.assigned_run(output_dir, config.get('run_id', ''))
    output_dir = run_directory / 'observations' / 'vision'
    selected = config.get('stage_prompts', {}).get('sense', {})
    prompt = (selected['text'] if config['prompt'] == selected.get('name') else
              files('smpa_ros').joinpath('prompts/sense/' + config['prompt'] + '.txt')
              .read_text(encoding='utf-8').strip())
    client = node.create_client(DescribeScene, '/sense') if describe is None else None
    subscription = None
    try:
        if client is not None:
            node.get_logger().info('Waiting for /sense service')
            if not client.wait_for_service(timeout_sec=timeout):
                raise TimeoutError(f'/sense is unavailable after {timeout:g}s')
        image_future = Future()

        def receive(image: Image) -> None:
            """Keep the first frame and its UTC receipt time; ignore later frames."""
            if not image_future.done():
                image_future.set_result((image, datetime.now(timezone.utc)))

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
        image, received_at = image_future.result()
        from PIL.PngImagePlugin import PngInfo
        import os
        timestamp = received_at.strftime('%Y-%m-%d_%H-%M-%S_%fZ')
        path = output_dir / f'{timestamp}_sense_output.md'
        metadata = PngInfo()
        camera = camera_provenance(image, config['camera_topic'], received_at)
        metadata.add_itxt('smpa.camera', json.dumps(camera))
        with path.with_suffix('.png').open('xb') as output:
            image_to_pil(image).save(output, format='PNG', pnginfo=metadata)
            output.flush()
            os.fsync(output.fileno())
        association = dict(cycle=config.get('_cycle'), sense_record=config.get('_sense_record'))
        append_observation(run_directory, modality='vision', kind='camera_frame',
            observed_at=received_at.isoformat(), status='evidence_without_description',
            files={'image': str(path.with_suffix('.png').relative_to(run_directory))},
            provenance=camera, **association)
        if refinement_settings(config)['enabled']:
            freeze_image(run_directory, image_to_pil(image), camera, **association)
        started = perf_counter()
        if describe is None:
            response_future = client.call_async(DescribeScene.Request(image=image, prompt=prompt))
            executor.spin_until_future_complete(response_future, timeout_sec=timeout)
            if not response_future.done():
                raise TimeoutError(f'/sense response timed out after {timeout:g}s')
            response = response_future.result()
            if response.error:
                raise RuntimeError(response.error)
            description = response.description
        else:
            description = describe(image, prompt)
        with path.open('x', encoding='utf-8', newline='') as output:
            output.write(description)
            output.flush()
            os.fsync(output.fileno())
        append_observation(run_directory, modality='vision', kind='scene_description',
            observed_at=received_at.isoformat(), status='generated', text=description,
            files={'image': str(path.with_suffix('.png').relative_to(run_directory)),
                   'text': str(path.relative_to(run_directory))}, provenance=camera, **association)
        node.get_logger().info(f'Markdown file saved: {path.resolve()}')
        return path
    except Exception as error:
        node.get_logger().error(f'Camera-to-Sense pipeline failed: {error}')
        raise
    finally:
        if subscription is not None:
            node.destroy_subscription(subscription)
        if client is not None:
            node.destroy_client(client)


def main(args=None):
    """Serve standalone visual requests until ROS shutdown or interruption.

    Construct one assigned SenseService, spin its existing executor and destroy
    owned resources on exit. Ignore a second SIGINT only during cleanup, restore
    the prior handler afterward, and propagate startup failures unchanged.
    """
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
