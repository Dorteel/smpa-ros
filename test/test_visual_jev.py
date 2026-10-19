from pathlib import Path
from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import EvaluateVisualDecision
import yaml

from smpa_ros.visual_jev.camera import evaluate_once
from smpa_ros.visual_jev.service import DEFAULT_QUESTION, VisualJevService, validate_request


def test_visual_jev_registry():
    root = Path(__file__).resolve().parents[1]
    entry = yaml.safe_load((root / 'models/model_library.yaml').read_text())['models']['visual-jev-4b']
    assert entry['base_model']['model_id'] == 'Qwen/Qwen3-VL-4B-Instruct'
    assert entry['adapter']['model_id'] == 'guanxuyu/visual-jev-4b-answer-sft'
    assert entry['base_model']['path'] == 'vision/visual-jev-4b/base_model'


@pytest.mark.parametrize('question,choices', [('', ['yes', 'no']), ('q', ['yes']),
    ('q', [''] * 2), ('q', ['yes', 'yes']), ('q', list(map(str, range(17))))])
def test_invalid_visual_request(question, choices):
    with pytest.raises(ValueError):
        validate_request(question, choices)


@pytest.fixture
def service():
    rclpy.init(args=[])
    backend = MagicMock()
    backend.evaluate.return_value = [0.75, 0.25]
    node = VisualJevService(backend=backend)
    try:
        yield node
    finally:
        node.destroy_node()
        rclpy.shutdown()


def request():
    return EvaluateVisualDecision.Request(
        image=Image(width=1, height=1, encoding='bgr8', step=3, data=[0, 0, 255]),
        context='Robot observation', question=DEFAULT_QUESTION, choices=['yes', 'no'],
    )


def test_visual_serialization_and_image(service):
    value = request()
    response = service.evaluate(value, EvaluateVisualDecision.Response())
    assert response.success and response.selected_answer == 'yes'
    assert list(response.answer_labels) == ['yes', 'no']
    assert list(response.probabilities) == pytest.approx([0.75, 0.25])
    args = service.backend.evaluate.call_args.args
    assert args[0].getpixel((0, 0)) == (255, 0, 0)
    assert args[1:] == (value.context, value.question, list(value.choices))


def test_visual_arbitrary_choices(service):
    value = request()
    value.question = 'What deserves investigation?'
    value.choices = ['person', 'object', 'nothing']
    service.backend.evaluate.return_value = [0.1, 0.7, 0.2]
    response = service.evaluate(value, EvaluateVisualDecision.Response())
    assert response.success and response.selected_answer == 'object'
    assert list(response.answer_labels) == list(value.choices)


@pytest.mark.parametrize('probabilities', [[1.0], [float('nan'), 0], [0.2, 0.2], [-1, 2]])
def test_visual_invalid_probabilities(service, probabilities):
    service.backend.evaluate.return_value = probabilities
    response = service.evaluate(request(), EvaluateVisualDecision.Response())
    assert not response.success
    assert 'probability distribution' in response.error_message


def test_visual_failure(service):
    service.backend.evaluate.side_effect = RuntimeError('inference failed')
    response = service.evaluate(request(), EvaluateVisualDecision.Response())
    assert not response.success and response.error_message == 'inference failed'
    value = request()
    value.image = Image()
    response = service.evaluate(value, EvaluateVisualDecision.Response())
    assert not response.success and 'encoding' in response.error_message


def exercise_camera(server, image):
    executor = SingleThreadedExecutor()
    camera = rclpy.create_node('visual_test_camera')
    client = rclpy.create_node('visual_test_client')
    try:
        for node in (server, camera, client):
            executor.add_node(node)
        topic = '/test/visual_jev_image'
        publisher = camera.create_publisher(Image, topic, qos_profile_sensor_data)
        camera.create_timer(0.05, lambda: publisher.publish(image))
        response = evaluate_once(client, executor, topic, timeout=300)
        assert response.success
        assert list(response.answer_labels) == ['yes', 'no']
        assert len(response.probabilities) == 2
        assert sum(response.probabilities) == pytest.approx(1, abs=1e-5)
        assert response.selected_answer in response.answer_labels
        return response
    finally:
        executor.shutdown()
        camera.destroy_node()
        client.destroy_node()


def test_visual_camera_round_trip(service):
    exercise_camera(service, request().image)
    service.backend.evaluate.assert_called_once()
