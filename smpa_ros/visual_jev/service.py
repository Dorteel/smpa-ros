"""Independent forced-choice visual evaluation at /visual_jev/evaluate."""
import math
from pathlib import Path
from time import perf_counter

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.node import Node
from smpa_ros_interfaces.srv import EvaluateVisualDecision
import yaml

from smpa_ros.backends.visual_jev import VisualJevBackend
from smpa_ros.sense import image_to_pil

DEFAULT_QUESTION = 'Does this image contain something interesting that deserves further investigation?'


def validate_request(question: str, choices: list[str]) -> None:
    """Require a nonempty question and two to sixteen distinct answer labels.

    Validate the supplied text without rewriting or reordering it. Raise a
    ValueError before inference when the forced-choice request is malformed.
    """
    if not question.strip():
        raise ValueError('question must not be empty')
    if not 2 <= len(choices) <= 16:
        raise ValueError('Provide 2 to 16 choices')
    if any(not choice.strip() for choice in choices) or len(set(choices)) != len(choices):
        raise ValueError('Choices must be nonempty and unique')


class VisualJevService(Node):
    """Own one evaluator and serve requests independently of robot cognition."""

    def __init__(self, backend=None):
        """Load the explicitly configured evaluator once, then expose its service.

        Use an injected backend for tests, or read the VisualJEV registry entry
        and local paths to construct the released evaluator on the chosen device.
        Initialization errors remain explicit. Create only the existing evaluation
        endpoint; do not invoke robot stages or assign an observation run.
        """
        super().__init__('visual_jev')
        self.declare_parameter('model', 'visual-jev-4b')
        self.declare_parameter('model_root', str(Path.cwd() / 'models'))
        self.declare_parameter('source_dir', str(Path.cwd() / 'third_party/Visual-Jev/code'))
        self.declare_parameter('device', 'cuda')
        self.declare_parameter('max_pixels', 200704)
        if backend is None:
            registry = Path(get_package_share_directory('smpa_ros')) / 'models/model_library.yaml'
            entry = yaml.safe_load(registry.read_text())['models'][self.get_parameter('model').value]
            root = Path(self.get_parameter('model_root').value)
            self.get_logger().info(f"Loading Visual Jev on {self.get_parameter('device').value}")
            started = perf_counter()
            try:
                backend = VisualJevBackend(
                    root / entry['base_model']['path'], root / entry['adapter']['path'],
                    Path(self.get_parameter('source_dir').value), self.get_parameter('device').value,
                    self.get_parameter('max_pixels').value,
                )
            except Exception as error:
                self.get_logger().error(f'Visual Jev initialization failed: {error}')
                raise
            self.get_logger().info(f'Visual Jev loaded in {perf_counter() - started:.2f}s; '
                                   f'memory before loading: {backend.memory}')
        self.backend = backend
        self.service = self.create_service(EvaluateVisualDecision, '/visual_jev/evaluate', self.evaluate)
        self.get_logger().info('/visual_jev/evaluate ready')

    def evaluate(self, request, response):
        """Validate and score one image request, returning ordered probabilities.

        Convert the ROS image with the shared modern converter, then pass its
        context, question and labels to the evaluator. Accept only a finite,
        normalized distribution and select its maximum in request order. Return
        errors through the existing response fields instead of crashing the node.
        """
        started = perf_counter()
        self.get_logger().info('Visual decision request received')
        try:
            choices = list(request.choices)
            validate_request(request.question, choices)
            probabilities = self.backend.evaluate(
                image_to_pil(request.image), request.context, request.question, choices,
            )
            if (len(probabilities) != len(choices)
                    or any(not math.isfinite(p) or p < 0 or p > 1 for p in probabilities)
                    or not math.isclose(sum(probabilities), 1, abs_tol=1e-5)):
                raise ValueError('Model returned an invalid probability distribution')
            response.answer_labels = choices
            response.probabilities = [float(p) for p in probabilities]
            response.selected_answer = choices[max(range(len(choices)), key=probabilities.__getitem__)]
            response.success = True
            self.get_logger().info(f'Visual decision completed in {perf_counter() - started:.2f}s')
        except Exception as error:
            response.error_message = str(error)
            self.get_logger().error(f'Visual decision failed: {error}')
        return response


def main(args=None):
    """Spin the independent ROS evaluator until shutdown or interruption.

    Initialize ROS, create the service and process requests. Always destroy the
    node and shut down its context, including after initialization failures.
    """
    rclpy.init(args=args)
    node = None
    try:
        node = VisualJevService()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
