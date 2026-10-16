import sys
from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import DescribeScene

from smpa_ros.sense.service import SenseService, image_to_pil
from smpa_ros.backends.transformers import TransformersVLM


def test_image_conversion_with_padding():
    image = Image(width=1, height=2, encoding='bgr8', step=4,
                  data=[0, 0, 255, 0, 0, 255, 0, 0])
    decoded = image_to_pil(image)
    assert list(decoded.getdata()) == [(255, 0, 0), (0, 255, 0)]


@pytest.mark.parametrize('image', [
    Image(), Image(width=1, height=1, encoding='rgb8', step=3, data=[0]),
    Image(width=1, height=1, encoding='rgb8', step=2, data=[0, 0]),
])
def test_invalid_image(image):
    with pytest.raises(ValueError):
        image_to_pil(image)


def test_vlm_processor_and_local_loading(monkeypatch):
    torch = MagicMock()
    transformers = MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    processor = transformers.AutoProcessor.from_pretrained.return_value
    processor.apply_chat_template.return_value = 'image prompt'
    processor.decode.return_value = ' A scene. '
    input_ids = MagicMock()
    input_ids.shape = (1, 4)
    processor.return_value.to.return_value = {'input_ids': input_ids}
    backend = TransformersVLM('cached/model')
    image = object()
    assert backend.describe(image, 'Describe.', max_new_tokens=7) == 'A scene.'
    transformers.AutoProcessor.from_pretrained.assert_called_once_with(
        'cached/model', local_files_only=True,
    )
    transformers.AutoModelForImageTextToText.from_pretrained.assert_called_once_with(
        'cached/model', local_files_only=True, dtype=torch.float16,
    )
    transformers.AutoModelForImageTextToText.from_pretrained.return_value.to.assert_called_once_with('cuda:0')
    processor.assert_called_once_with(
        text='image prompt', images=[image], return_tensors='pt', add_special_tokens=False,
    )
    backend.model.generate.assert_called_once_with(
        input_ids=input_ids, max_new_tokens=7, do_sample=False,
    )
    backend.model.generate.return_value.__getitem__.assert_called_once_with((0, slice(4, None)))
    torch.no_grad.assert_called_once_with()


def test_sense_service_round_trip():
    """Use real ROS service serialization/discovery with a mock VLM only."""
    rclpy.init(args=[])
    executor = SingleThreadedExecutor()
    server = client_node = None
    try:
        backend = MagicMock()
        backend.describe.return_value = 'A red pixel.'
        server = SenseService(backend=backend)
        client_node = rclpy.create_node('sense_test_client')
        executor.add_node(server)
        executor.add_node(client_node)
        client = client_node.create_client(DescribeScene, '/sense')
        assert client.wait_for_service(timeout_sec=5), 'Sense service was not discovered'
        image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
        for prompt, expected in [('', 'Describe the scene.'), ('What color?', 'What color?')]:
            future = client.call_async(DescribeScene.Request(image=image, prompt=prompt))
            executor.spin_until_future_complete(future, timeout_sec=10)
            assert future.done(), 'Sense request timed out'
            response = future.result()
            assert response.error == ''
            assert response.description == 'A red pixel.'
            args, kwargs = backend.describe.call_args
            assert args[0].getpixel((0, 0)) == (255, 0, 0)
            assert args[1] == expected
            assert kwargs == {'max_new_tokens': 128}
        future = client.call_async(DescribeScene.Request())
        executor.spin_until_future_complete(future, timeout_sec=10)
        assert future.done()
        assert future.result().error
        assert future.result().description == ''
        assert backend.describe.call_count == 2
    finally:
        executor.shutdown()
        if client_node is not None:
            client_node.destroy_node()
        if server is not None:
            server.destroy_node()
        rclpy.shutdown()
