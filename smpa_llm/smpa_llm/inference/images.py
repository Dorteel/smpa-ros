"""Decode ROS camera evidence once for shared inference and legacy Sense."""
from PIL import Image as PILImage
from sensor_msgs.msg import Image


def image_to_pil(image: Image) -> PILImage.Image:
    """Validate a ROS image and return its original decoded RGB pixels.

    Select the raw channel order from the supported 8-bit encoding and check
    dimensions, row stride and buffer length before decoding. Respect padding
    between rows, convert to RGB, and reject unsupported or malformed input
    rather than passing ambiguous pixels to inference or persistence.
    """
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

