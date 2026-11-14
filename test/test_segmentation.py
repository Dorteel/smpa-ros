"""Check segmentation serialization and inference behavior without downloading weights."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import SegmentImage
torch = pytest.importorskip('torch')

from smpa_ros.segmentation import SegmentationService


@pytest.fixture
def clipseg_weights(tmp_path, monkeypatch):
    """Substitute only the model loader, retaining real Torch and ROS messages.

    Create a local directory and a minimal processor result so startup follows
    the production loading path. Supply fixed logits with scores of 0.2 and
    0.8, allowing tests to predict interpolation and threshold behavior. The
    fake model checks that every forward call runs with gradients disabled.
    Return both constructors so repeated requests can prove weights load once.
    """
    transformers = pytest.importorskip('transformers')
    directory = tmp_path / 'clipseg'
    directory.mkdir()
    processor = MagicMock()
    processor.return_value.to.return_value = {'pixel_values': torch.zeros(1, 3, 2, 2)}
    model = MagicMock()
    model.to.return_value = model
    model.device = 'cpu'
    model.config.vision_config.image_size = 352

    def infer(**inputs):
        """Return known logits and verify the service disables gradient tracking.

        Production inference passes tensors from the processor to this call.
        The symmetric score pattern exposes any transposed axes, normalization,
        or thresholding before resizing in the returned mask.
        """
        assert torch.is_inference_mode_enabled()
        return SimpleNamespace(logits=torch.logit(torch.tensor([[[0.2, 0.8], [0.2, 0.8]]])))

    model.side_effect = infer
    processor_loader = MagicMock(return_value=processor)
    model_loader = MagicMock(return_value=model)
    monkeypatch.setattr(transformers.CLIPSegProcessor, 'from_pretrained', processor_loader)
    monkeypatch.setattr(transformers.CLIPSegForImageSegmentation, 'from_pretrained', model_loader)
    return directory, processor, model, processor_loader, model_loader


@pytest.mark.parametrize('encoding,pixels', [('rgb8', [255, 0, 0]), ('bgr8', [0, 0, 255])])
@pytest.mark.parametrize('threshold', [0.5, 0.7])
def test_segmentation_round_trip_and_one_load(clipseg_weights, encoding, pixels, threshold):
    """Send repeated real ROS requests and verify aligned little-endian masks.

    Use a non-square, padded input to check the existing image decoder.
    Compare resized scores with direct bilinear interpolation, then verify
    binary values and copied headers. The second request must reuse both
    startup objects; it must not load weights again or normalize score ranges.
    """
    directory, processor, model, processor_loader, model_loader = clipseg_weights
    rclpy.init(args=['--ros-args', '-p', f'model_path:={directory}', '-p', 'device:=cpu',
                     '-p', f'threshold:={threshold}'])
    server = client_node = None
    executor = SingleThreadedExecutor()
    try:
        server = SegmentationService()
        client_node = rclpy.create_node('test_segment_client')
        executor.add_node(server)
        executor.add_node(client_node)
        client = client_node.create_client(SegmentImage, '/segment_image')
        assert client.wait_for_service(timeout_sec=5)
        image = Image(width=5, height=3, encoding=encoding, step=16,
                      data=(pixels * 5 + [99]) * 3)
        image.header.frame_id = 'camera'
        image.header.stamp.sec = 7
        for prompt in ('green shirt', 'blue trousers'):
            future = client.call_async(SegmentImage.Request(image=image, prompt=prompt))
            executor.spin_until_future_complete(future, timeout_sec=10)
            assert future.done()
            response = future.result()
            assert response.success, response.message
            assert response.soft_mask.encoding == '32FC1' and response.soft_mask.step == 20
            assert response.soft_mask.is_bigendian == 0
            assert response.binary_mask.encoding == 'mono8' and response.binary_mask.step == 5
            for mask in (response.soft_mask, response.binary_mask):
                assert (mask.width, mask.height) == (5, 3)
                assert mask.header == image.header
            import numpy as np
            scores = np.frombuffer(response.soft_mask.data, dtype='<f4').reshape(3, 5)
            expected = torch.nn.functional.interpolate(torch.tensor([[[[.2, .8], [.2, .8]]]]),
                size=(3, 5), mode='bilinear', align_corners=False)[0, 0].numpy()
            np.testing.assert_allclose(scores, expected, atol=1e-6)
            assert scores.min() == pytest.approx(.2) and scores.max() == pytest.approx(.8)
            assert bytes(response.binary_mask.data) == ((scores >= threshold).astype('uint8') * 255).tobytes()
            options = processor.call_args.kwargs
            assert options['images'].getpixel((0, 0)) == (255, 0, 0)
            assert options['text'] == [prompt] and options['do_center_crop'] is False
            assert options['size'] == {'height':352, 'width':352}
        processor_loader.assert_called_once_with(str(directory), local_files_only=True)
        model_loader.assert_called_once_with(str(directory), local_files_only=True, dtype=torch.float32)
        assert model.call_count == 2
    finally:
        executor.shutdown()
        if server is not None:
            server.destroy_node()
        if client_node is not None:
            client_node.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('prompt,image', [
    (' ', Image(width=1, height=1, encoding='rgb8', step=3, data=[0, 0, 0])),
    ('shirt', Image()),
    ('shirt', Image(width=1, height=1, encoding='32FC1', step=4, data=[0, 0, 0, 0])),
    ('shirt', Image(width=1, height=1, encoding='rgb8', step=3, data=[0])),
])
def test_invalid_request_does_not_crash(clipseg_weights, prompt, image):
    """Reject invalid input without inference, then handle a valid request.

    Failed responses must contain no old masks and must explain the problem.
    A subsequent successful request demonstrates the node stays usable after
    an input error. The mocked loader still represents a single startup load.
    """
    directory, processor, model, processor_loader, model_loader = clipseg_weights
    rclpy.init(args=['--ros-args', '-p', f'model_path:={directory}', '-p', 'device:=cpu'])
    server = None
    try:
        server = SegmentationService()
        response = server.segment(SegmentImage.Request(image=image, prompt=prompt), SegmentImage.Response())
        assert not response.success and response.message and not response.soft_mask.data and not response.binary_mask.data
        model.assert_not_called()
        valid = Image(width=1, height=1, encoding='mono8', step=1, data=[127])
        assert server.segment(SegmentImage.Request(image=valid, prompt='object'), SegmentImage.Response()).success
        model_loader.assert_called_once()
    finally:
        if server is not None:
            server.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('threshold', [-0.1, 1.1, float('nan'), float('inf')])
def test_invalid_threshold_before_loading(clipseg_weights, threshold):
    """Reject unusable thresholds at startup without allocating model weights.

    Both values outside the score range and nonfinite values are invalid.
    Destroy the partially initialized node after the expected exception so
    this test leaves no ROS resources behind.
    """
    directory, processor, model, processor_loader, model_loader = clipseg_weights
    import yaml
    encoded_threshold = yaml.safe_dump(threshold).splitlines()[0]
    rclpy.init(args=['--ros-args', '-p', f'model_path:={directory}', '-p', f'threshold:={encoded_threshold}'])
    server = SegmentationService.__new__(SegmentationService)
    try:
        with pytest.raises(ValueError, match='threshold'):
            server.__init__()
        model_loader.assert_not_called()
    finally:
        server.destroy_node()
        rclpy.shutdown()


def test_inference_failure_and_recovery(clipseg_weights):
    """Turn model exceptions and nonfinite scores into failed responses.

    Neither kind of failure may crash the service or trigger another weight
    load. Restore the original inference call afterward and confirm that a
    valid request can still succeed using the same model.
    """
    directory, processor, model, processor_loader, model_loader = clipseg_weights
    rclpy.init(args=['--ros-args', '-p', f'model_path:={directory}', '-p', 'device:=cpu'])
    server = None
    try:
        server = SegmentationService()
        image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
        request = SegmentImage.Request(image=image, prompt='red')
        original = model.side_effect
        model.side_effect = RuntimeError('inference failed')
        assert not server.segment(request, SegmentImage.Response()).success
        model.side_effect = None
        model.return_value = SimpleNamespace(logits=torch.full((1, 2, 2), float('nan')))
        assert not server.segment(request, SegmentImage.Response()).success
        model.side_effect = original
        assert server.segment(request, SegmentImage.Response()).success
        model_loader.assert_called_once()
    finally:
        if server is not None:
            server.destroy_node()
        rclpy.shutdown()


def test_client_overlay_preserves_unselected_pixels(tmp_path):
    """Write inspection files without running a publisher or a model.

    Import the small example client and give it a blue image whose first
    pixel is selected. The saved mask must retain its binary values; only
    the selected overlay pixel may change. A mismatched mask must be rejected.
    """
    from PIL import Image as PILImage
    source = Path(__file__).resolve().parents[1] / 'scripts/segment_image_client.py'
    spec = importlib.util.spec_from_file_location('segment_client_test', source)
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    image = Image(width=2, height=1, encoding='rgb8', step=6, data=[0, 0, 255] * 2)
    mask = Image(width=2, height=1, encoding='mono8', step=2, data=[255, 0])
    client.save_masks(image, mask, tmp_path)
    assert PILImage.open(tmp_path / 'binary_mask.png').tobytes() == bytes([255, 0])
    overlay = PILImage.open(tmp_path / 'overlay.png')
    assert overlay.getpixel((0, 0)) != (0, 0, 255) and overlay.getpixel((1, 0)) == (0, 0, 255)
    mask.width = 1
    mask.step = 1
    mask.data = [255]
    with pytest.raises(ValueError, match='dimensions'):
        client.save_masks(image, mask, tmp_path)


@pytest.mark.parametrize('requested,available,expected', [('cpu',False,'cpu'), ('auto',False,'cpu'),
                                                        ('auto',True,'cuda'), ('cuda',True,'cuda')])
def test_device_selection_reuses_startup_model(clipseg_weights, monkeypatch, requested, available, expected):
    """Resolve device selection without allocating actual CUDA memory in tests.

    Substitute CUDA availability while retaining the mocked weight loader.
    Explicit CPU must stay on CPU and automatic selection must follow the
    availability check. The single loaded model moves once to the selected
    device, remains in evaluation mode, and is then released with the node.
    """
    directory, processor, model, processor_loader, model_loader = clipseg_weights
    monkeypatch.setattr(torch.cuda, 'is_available', MagicMock(return_value=available))
    rclpy.init(args=['--ros-args', '-p', f'model_path:={directory}', '-p', f'device:={requested}'])
    server = None
    try:
        server = SegmentationService()
        model.to.assert_called_once_with(expected)
        model.eval.assert_called_once()
        model_loader.assert_called_once()
    finally:
        if server is not None:
            server.destroy_node()
        rclpy.shutdown()


def test_explicit_cuda_unavailable_does_not_load(clipseg_weights, monkeypatch):
    """Fail explicit CUDA startup rather than silently selecting CPU.

    Simulate a machine without CUDA and request CUDA anyway. The explanatory
    error must occur before loading weights, and the partial node is cleaned
    up just as it is in other startup-failure tests.
    """
    directory, processor, model, processor_loader, model_loader = clipseg_weights
    monkeypatch.setattr(torch.cuda, 'is_available', MagicMock(return_value=False))
    rclpy.init(args=['--ros-args', '-p', f'model_path:={directory}', '-p', 'device:=cuda'])
    server = SegmentationService.__new__(SegmentationService)
    try:
        with pytest.raises(RuntimeError, match='CUDA was requested'):
            server.__init__()
        model_loader.assert_not_called()
    finally:
        server.destroy_node()
        rclpy.shutdown()


def test_missing_local_weights_do_not_contact_hub(clipseg_weights, tmp_path):
    """Reject a nonexistent local directory without invoking a model loader.

    A misspelled or missing local model selection must never become a Hugging
    Face repository download. Confirm that both loaders remain untouched and
    the startup error points the user to the explicit download instructions.
    """
    directory, processor, model, processor_loader, model_loader = clipseg_weights
    rclpy.init(args=['--ros-args', '-p', f'model_path:={tmp_path / "missing"}'])
    server = SegmentationService.__new__(SegmentationService)
    try:
        with pytest.raises(FileNotFoundError, match='explicit download'):
            server.__init__()
        processor_loader.assert_not_called()
        model_loader.assert_not_called()
    finally:
        server.destroy_node()
        rclpy.shutdown()
