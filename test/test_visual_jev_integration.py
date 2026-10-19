import importlib.util
import os
from pathlib import Path
import resource
from time import perf_counter

from PIL import Image as PILImage
import pytest
import rclpy
from sensor_msgs.msg import Image

from smpa_ros.backends.visual_jev import VisualJevBackend
from smpa_ros.visual_jev.service import VisualJevService
from test_visual_jev import exercise_camera


@pytest.mark.integration
def test_released_visual_jev_camera():
    root = Path(__file__).resolve().parents[1]
    library = root / 'models/vision/visual-jev-4b'
    if not (library / 'adapter/adapter_model.safetensors').is_file():
        pytest.skip('Visual Jev adapter is not downloaded; use download_models.py --visual-jev')
    if importlib.util.find_spec('peft') is None:
        pytest.skip('PEFT is unavailable; use the documented .venv')
    device = os.environ.get('SMPA_VISUAL_JEV_DEVICE', 'cpu')
    started = perf_counter()
    backend = VisualJevBackend(library / 'base_model', library / 'adapter',
                              root / 'third_party/Visual-Jev/code', device=device)
    print(f'Visual Jev initialization: {perf_counter() - started:.2f}s; {backend.memory}')
    rclpy.init(args=[])
    server = VisualJevService(backend=backend)
    try:
        with PILImage.open(root / 'third_party/Visual-Jev/assets/figures/demo_bottle.jpg') as photo:
            photo = photo.convert('RGB')
            image = Image(width=photo.width, height=photo.height, encoding='rgb8',
                          step=photo.width * 3, data=photo.tobytes())
        response = exercise_camera(server, image)
        assert backend.last_logits.numel() == 2
        print('Real image decision:', response.selected_answer, list(response.probabilities))
        print('Peak process RSS MiB:', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    finally:
        server.destroy_node()
        rclpy.shutdown()
