from importlib.resources import files
from pathlib import Path
from time import perf_counter

from ament_index_python.packages import get_package_share_directory
from PIL import Image as PILImage
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import DescribeScene
import yaml

from smpa_ros.backends.transformers import TransformersVLM


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
        self.get_logger().info('Sense node starting')
        share = Path(get_package_share_directory('smpa_ros'))
        self.declare_parameter('config', str(share / 'config/pipeline.yaml'))
        pipeline = yaml.safe_load(Path(self.get_parameter('config').value).read_text())
        self.declare_parameter('model_config', pipeline['model'] + '.yaml')
        self.declare_parameter('dtype', 'float16')
        self.declare_parameter('max_new_tokens', 128)
        self.max_new_tokens = self.get_parameter('max_new_tokens').value
        if self.max_new_tokens <= 0:
            raise ValueError('max_new_tokens must be positive')
        self.default_prompt = files('smpa_ros.sense').joinpath(
            'prompts/describe_scene.txt',
        ).read_text(encoding='utf-8').strip()
        if backend is None:
            config_path = Path(self.get_parameter('model_config').value)
            if not config_path.is_absolute():
                config_path = Path(get_package_share_directory('smpa_ros')) / 'models' / config_path
            config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
            self.get_logger().info(
                f"Selected model: {config['model_id']}; backend: TransformersVLM; "
                f"dtype: {self.get_parameter('dtype').value}",
            )
            self.get_logger().info('Loading model weights from the local cache')
            started = perf_counter()
            try:
                backend = TransformersVLM(config['model_id'], self.get_parameter('dtype').value)
            except Exception as error:
                self.get_logger().error(f'Model loading failed: {error}')
                raise
            self.get_logger().info(f'Model loaded successfully in {perf_counter() - started:.2f}s')
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
            image = image_to_pil(request.image)
            # Bound image size before processor tiling to limit inference memory.
            image.thumbnail((512, 512))
            response.description = self.backend.describe(
                image, request.prompt.strip() or self.default_prompt,
                max_new_tokens=self.max_new_tokens,
            )
            self.get_logger().info(f'Inference completed in {perf_counter() - started:.2f}s')
        except Exception as error:
            response.error = str(error)
            self.get_logger().error(
                f'Sense request failed after {perf_counter() - started:.2f}s: {error}',
            )
        return response


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = SenseService()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
