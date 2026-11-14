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


def test_visual_backend_uses_only_supplied_option_logits():
    """Verify the wrapper's real probability calculation without loading weights.

    Supply upstream-style logits with an extra padded slot. Require the released
    independent path, unchanged question/context and choice order, and softmax
    over only the requested options. Preserve raw logits for later inspection.
    """
    from types import SimpleNamespace
    import torch
    from smpa_ros.backends.visual_jev import VisualJevBackend
    backend = VisualJevBackend.__new__(VisualJevBackend)
    backend.vdm = MagicMock()
    group = SimpleNamespace(n_options=[2])
    backend.vdm.prepare_group.return_value = group
    logits = torch.tensor([[2.0, 1.0, 99.0]])
    backend.vdm.run_independent.return_value = {'lm_option_logits': logits}
    image = object()
    result = backend.evaluate(image, 'untrusted context', 'Choose?', ['left', 'right'])
    backend.vdm.prepare_group.assert_called_once_with(image,
        [{'qtype': 'choice', 'instruction': 'Choose?', 'candidates': ['left', 'right']}],
        shared_context='untrusted context')
    backend.vdm.run_independent.assert_called_once_with(group)
    assert result == pytest.approx(torch.softmax(logits[0, :2], dim=-1).tolist())
    assert backend.last_logits.tolist() == [2.0, 1.0]


def test_visual_launch_remains_independent(tmp_path, monkeypatch):
    """Launch exactly one evaluator while retaining the existing robot default.

    Execute the real launch callback with explicit visual selection and inspect
    its node parameters. Check that the declared default still selects robot
    vision, so integration cannot silently replace the normal cognitive workflow.
    """
    import importlib.util
    import sys
    from launch import LaunchContext
    from launch.actions import DeclareLaunchArgument
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('visual_integration_launch', root / 'launch/llm.launch.py')
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)
    constructor = MagicMock()
    monkeypatch.setattr(launch, 'Node', constructor)
    context = LaunchContext()
    context.launch_configurations.update(service='visual_jev', model='visual-jev-4b',
        device='cpu', model_root=str(tmp_path), source_dir=str(root / 'third_party/Visual-Jev/code'),
        max_pixels='200704', python=sys.executable)
    assert launch.start(context) == [constructor.return_value]
    assert constructor.call_count == 1
    assert constructor.call_args.kwargs['executable'] == 'visual_jev'
    assert constructor.call_args.kwargs['parameters'][0]['model_root'] == str(tmp_path)
    description = launch.generate_launch_description()
    selection = next(item for item in description.entities
                     if isinstance(item, DeclareLaunchArgument) and item.name == 'service')
    assert selection.default_value[0].text == 'vision'


def test_visual_camera_reads_canonical_installed_configuration(tmp_path, monkeypatch, capsys):
    """Use config.yaml for the independent camera client and clean up its context.

    Provide only the canonical installed file, mock transport at the evaluator
    boundary, and require its camera topic and unchanged JSON output fields.
    Verify executor/node cleanup without creating any persisted robot observation.
    """
    from types import SimpleNamespace
    import json
    from smpa_ros.visual_jev import camera
    (tmp_path / 'config.yaml').write_text('camera_topic: /canonical/test_camera\n')
    monkeypatch.setattr(camera, 'get_package_share_directory', lambda name: str(tmp_path))
    node, executor = MagicMock(), MagicMock()
    monkeypatch.setattr(camera.rclpy, 'init', MagicMock())
    monkeypatch.setattr(camera.rclpy, 'create_node', MagicMock(return_value=node))
    monkeypatch.setattr(camera.rclpy, 'ok', lambda: True)
    shutdown = MagicMock()
    monkeypatch.setattr(camera.rclpy, 'shutdown', shutdown)
    monkeypatch.setattr(camera, 'SingleThreadedExecutor', MagicMock(return_value=executor))
    evaluate = MagicMock(return_value=SimpleNamespace(selected_answer='yes',
        answer_labels=['yes', 'no'], probabilities=[0.75, 0.25]))
    monkeypatch.setattr(camera, 'evaluate_once', evaluate)
    camera.main(args=['visual_jev_camera'])
    assert evaluate.call_args.args[2] == '/canonical/test_camera'
    assert json.loads(capsys.readouterr().out) == {
        'selected_answer': 'yes', 'probabilities': {'yes': 0.75, 'no': 0.25}}
    executor.shutdown.assert_called_once()
    node.destroy_node.assert_called_once()
    shutdown.assert_called_once()
    assert set(tmp_path.iterdir()) == {tmp_path / 'config.yaml'}
