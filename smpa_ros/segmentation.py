"""Serve text-guided segmentation independently of the cognitive stages.

Concepts:
    A soft mask contains CLIPSeg sigmoid scores between zero and one. These
    scores are model evidence, not calibrated probabilities. A binary mask
    marks scores at or above the configured threshold with 255 and others
    with zero. Neither mask is normalized relative to the current image.
"""
from copy import deepcopy
import math
from pathlib import Path

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import SegmentImage

from smpa_ros.sense import image_to_pil


class SegmentationService(Node):
    """Own one CLIPSeg model and answer image-and-text requests sequentially."""

    def __init__(self):
        """Validate startup settings, then load one local processor and model.

        The model path follows the repository's models/vision layout. The
        device parameter chooses CPU, CUDA, or automatic CUDA detection.
        The threshold must be finite and between zero and one. Invalid
        settings, unavailable explicitly requested CUDA, or missing local
        weights stop startup with an actionable error; no download occurs.
        Once loaded, the model stays in evaluation mode and is shared by all
        requests to /segment_image. No cognitive backend is instantiated.
        """
        super().__init__('segment_image')
        self.declare_parameter('model_path', str(Path.cwd() / 'models/vision/clipseg_rd64_refined'))
        self.declare_parameter('device', 'auto')
        self.declare_parameter('threshold', 0.5)
        self.threshold = self.get_parameter('threshold').value
        if (type(self.threshold) not in (int, float) or not math.isfinite(self.threshold)
                or not 0 <= self.threshold <= 1):
            raise ValueError('threshold must be finite and between 0 and 1')
        device = self.get_parameter('device').value
        if device not in ('auto', 'cpu', 'cuda'):
            raise ValueError('device must be auto, cpu, or cuda')
        directory = Path(self.get_parameter('model_path').value).expanduser().resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f'CLIPSeg directory is missing: {directory}. '
                                    'See README.md for the explicit download command.')
        import torch
        from transformers import CLIPSegForImageSegmentation, CLIPSegProcessor

        self.torch = torch
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA was requested but is unavailable; choose device:=cpu or fix CUDA')
        if device == 'auto':
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.processor = CLIPSegProcessor.from_pretrained(str(directory), local_files_only=True)
        self.model = CLIPSegForImageSegmentation.from_pretrained(
            str(directory), local_files_only=True, dtype=torch.float32,
        ).to(device)
        self.model.eval()
        self.service = self.create_service(SegmentImage, '/segment_image', self.segment)
        self.get_logger().info(f'CLIPSeg loaded once from {directory} on {device} (float32); '
                               f'threshold={self.threshold}; /segment_image ready')

    def segment(self, request, response):
        """Return aligned soft scores and a thresholded mask for one request.

        Reject an empty prompt or an invalid image before inference. Reuse
        the existing image decoder and the model loaded during startup.
        Resize the entire image to the model's input square without center
        cropping, so every input pixel remains represented. In inference
        mode, obtain the score map, apply sigmoid without normalization,
        and bilinearly resize it to the original height and width. Reject
        nonfinite scores instead of returning a misleading mask.

        Encode soft scores as little-endian 32FC1 and apply the threshold
        only after resizing to produce mono8 values of zero or 255. Both
        messages retain the input header. A failed request returns empty
        masks, success false, and an explanation; the service remains alive.
        """
        response.success = False
        try:
            if not request.prompt.strip():
                raise ValueError('prompt must not be empty')
            image = image_to_pil(request.image)
            size = self.model.config.vision_config.image_size
            inputs = self.processor(text=[request.prompt], images=image, padding=True,
                                    return_tensors='pt', do_center_crop=False,
                                    size={'height': size, 'width': size}).to(self.model.device)
            with self.torch.inference_mode():
                logits = self.model(**inputs).logits
                if logits.ndim != 3 or logits.shape[0] != 1:
                    raise ValueError('CLIPSeg must return one two-dimensional score map')
                scores = logits.float().sigmoid().unsqueeze(1)
                scores = self.torch.nn.functional.interpolate(
                    scores, size=(image.height, image.width), mode='bilinear', align_corners=False,
                )[0, 0].cpu().contiguous()
                if not self.torch.isfinite(scores).all().item():
                    raise ValueError('CLIPSeg returned nonfinite scores')
                binary = (scores >= self.threshold).to(self.torch.uint8) * 255
            response.soft_mask = Image(header=deepcopy(request.image.header), height=image.height,
                width=image.width, encoding='32FC1', is_bigendian=0, step=image.width * 4,
                data=scores.numpy().astype('<f4', copy=False).tobytes())
            response.binary_mask = Image(header=deepcopy(request.image.header), height=image.height,
                width=image.width, encoding='mono8', is_bigendian=0, step=image.width,
                data=binary.numpy().tobytes())
            response.success = True
            response.message = 'Segmentation completed; soft scores are not calibrated probabilities'
        except Exception as error:
            response.soft_mask = Image()
            response.binary_mask = Image()
            response.message = f'{type(error).__name__}: {error}'
            self.get_logger().error(f'Segmentation request failed: {response.message}')
        return response


def main(args=None):
    """Start the standalone service and release ROS resources when it stops.

    ROS arguments configure the startup parameters. The single-threaded
    spin handles requests sequentially using the same loaded model. Normal
    shutdown interrupts are quiet, while startup failures remain visible.
    Destroy the node when constructed and always shut down its ROS context.
    """
    node = None
    rclpy.init(args=args)
    try:
        node = SegmentationService()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
