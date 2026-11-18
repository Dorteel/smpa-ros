"""Turn sensor messages into compact payloads a browser can draw.

Concepts:
    The sensor registry maps a ROS message type to a viewer name and an
    encoder. A viewer is the frontend module that draws the data. An encoder
    reduces one message to a small JSON header and, for images and point
    clouds, one block of binary data. Supporting a new sensor type means
    adding one encoder here and one viewer module in the frontend.

    Encoders accept any object with the message's attributes, so they can be
    tested without ROS. An encoding the gateway cannot draw raises
    ``UnsupportedData`` with an explanation instead of failing the stream.
"""
import io
import math

MAX_POINTS = 20000
MAX_SCAN_POINTS = 1440
POINT_FIELD_DTYPES = {1: 'i1', 2: 'u1', 3: 'i2', 4: 'u2', 5: 'i4', 6: 'u4', 7: 'f4', 8: 'f8'}
RAW_IMAGE_MODES = {'rgb8': ('RGB', 'RGB', 3), 'bgr8': ('RGB', 'BGR', 3), 'rgba8': ('RGBA', 'RGBA', 4),
                   'bgra8': ('RGBA', 'BGRA', 4), 'mono8': ('L', 'L', 1), '8uc1': ('L', 'L', 1)}
SCALED_IMAGE_DTYPES = {'mono16': 'u2', '16uc1': 'u2', '32fc1': 'f4'}


class UnsupportedData(ValueError):
    """The message is valid ROS data, but this gateway cannot visualise it."""


def stamp_seconds(header):
    """Return a message header's time stamp as seconds, for display only."""
    return header.stamp.sec + header.stamp.nanosec / 1e9


def finite(value):
    """Return a number unchanged, or ``None`` when JSON cannot express it."""
    value = float(value)
    return value if math.isfinite(value) else None


def numbers(values):
    """Convert a numeric sequence to a list of finite numbers or nulls."""
    return [finite(value) for value in values]


def header_fields(message):
    """Describe the frame and time every stamped sensor message carries."""
    return dict(frame_id=message.header.frame_id, stamp=stamp_seconds(message.header))


def jpeg_bytes(image, max_width, quality=80):
    """Shrink a picture to the requested width and compress it as JPEG.

    Narrower pictures are not enlarged. Transparency is dropped, because
    JPEG has none. The result is the complete JPEG file as bytes.
    """
    if image.width > max_width:
        image = image.resize((max_width, max(1, round(image.height * max_width / image.width))))
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=quality)
    return buffer.getvalue()


def encode_image(message, max_width=960):
    """Convert an uncompressed ``sensor_msgs/Image`` to a JPEG and a description.

    Eight-bit colour and grey encodings are decoded directly, honouring row
    padding. Sixteen-bit and floating-point single-channel images (typical
    depth images) are stretched between their smallest and largest finite
    value so that structure is visible; that changes their meaning from
    distance to contrast, which the header states as ``scaled``. Inconsistent
    sizes or any other encoding raise ``UnsupportedData``.
    """
    import numpy
    from PIL import Image

    encoding = message.encoding.lower()
    width, height, step = message.width, message.height, message.step
    data = bytes(message.data)
    if width == 0 or height == 0 or len(data) != step * height:
        raise UnsupportedData('Image dimensions, row length and data size do not agree')
    scaled = False
    if encoding in RAW_IMAGE_MODES:
        mode, raw_mode, channels = RAW_IMAGE_MODES[encoding]
        if step < width * channels:
            raise UnsupportedData('Image row length is too short for its width')
        image = Image.frombytes(mode, (width, height), data, 'raw', raw_mode, step, 1)
    elif encoding in SCALED_IMAGE_DTYPES:
        kind = SCALED_IMAGE_DTYPES[encoding]
        order = '>' if message.is_bigendian else '<'
        size = int(kind[1])
        if step < width * size:
            raise UnsupportedData('Image row length is too short for its width')
        rows = numpy.frombuffer(data, dtype=order + kind).reshape(height, step // size)[:, :width]
        values = rows.astype('f4')
        valid = numpy.isfinite(values)
        low, high = (float(values[valid].min()), float(values[valid].max())) if valid.any() else (0.0, 1.0)
        span = (high - low) or 1.0
        grey = numpy.where(valid, (values - low) / span * 255.0, 0.0).astype('u1')
        image = Image.fromarray(grey, 'L')
        scaled = True
    else:
        raise UnsupportedData(f'Image encoding "{message.encoding}" is not supported by the viewer')
    return (dict(header_fields(message), binary='jpeg', width=width, height=height,
                 encoding=message.encoding, scaled=scaled), jpeg_bytes(image, max_width))


def encode_compressed_image(message, max_width=960):
    """Pass a compressed image to the browser as JPEG.

    JPEG data is forwarded untouched, which costs nothing. PNG and other
    formats Pillow can read are re-encoded as JPEG. Data Pillow cannot read,
    such as the ``compressedDepth`` transport, raises ``UnsupportedData``.
    """
    from PIL import Image

    data = bytes(message.data)
    described = dict(header_fields(message), binary='jpeg', format=message.format, scaled=False)
    if data[:2] == b'\xff\xd8':
        return described, data
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:
        raise UnsupportedData(f'Compressed image format "{message.format}" cannot be decoded') from error
    return dict(described, width=image.width, height=image.height), jpeg_bytes(image, max_width)


def encode_point_cloud(message, max_points=MAX_POINTS):
    """Reduce a ``PointCloud2`` to at most ``max_points`` positions with optional colour.

    Read the x, y and z fields from the packed point records. Points with a
    non-finite coordinate are dropped. If more points remain than allowed,
    keep every n-th point so the shape is preserved. A packed ``rgb`` or
    ``rgba`` field gives per-point colour; otherwise an ``intensity`` field
    gives a grey level. The binary block holds 32-bit floats, three per point
    (x, y, z) or six per point (x, y, z, r, g, b with colour between 0 and 1);
    the header's ``stride`` says which. A cloud without x, y and z raises
    ``UnsupportedData``.
    """
    import numpy

    fields = {field.name: field for field in message.fields}
    if not all(name in fields for name in ('x', 'y', 'z')):
        raise UnsupportedData('Point cloud has no x, y and z fields')
    order = '>' if message.is_bigendian else '<'
    wanted = ['x', 'y', 'z'] + [name for name in ('rgb', 'rgba', 'intensity') if name in fields]
    names, formats, offsets = [], [], []
    for name in wanted:
        field = fields[name]
        if field.datatype not in POINT_FIELD_DTYPES:
            raise UnsupportedData(f'Point field "{name}" has an unknown datatype')
        names.append(name)
        formats.append(order + POINT_FIELD_DTYPES[field.datatype])
        offsets.append(field.offset)
    dtype = numpy.dtype(dict(names=names, formats=formats, offsets=offsets, itemsize=message.point_step))
    total = message.width * message.height
    data = bytes(message.data)
    if message.point_step == 0 or len(data) < total * message.point_step:
        raise UnsupportedData('Point cloud data is shorter than its declared size')
    points = numpy.frombuffer(data, dtype=dtype, count=total)
    xyz = numpy.stack([points['x'], points['y'], points['z']], axis=1).astype('f4')
    keep = numpy.isfinite(xyz).all(axis=1)
    points, xyz = points[keep], xyz[keep]
    valid = len(xyz)
    if valid > max_points:
        step = math.ceil(valid / max_points)
        points, xyz = points[::step], xyz[::step]
    colour_source = next((name for name in ('rgb', 'rgba', 'intensity') if name in fields), None)
    if colour_source in ('rgb', 'rgba'):
        packed = numpy.ascontiguousarray(points[colour_source]).view('u4' if order == '<' else '>u4')
        packed = packed.astype('u4')
        colour = numpy.stack([(packed >> 16) & 255, (packed >> 8) & 255, packed & 255], axis=1) / 255.0
        block = numpy.hstack([xyz, colour.astype('f4')])
    elif colour_source == 'intensity' and len(xyz):
        intensity = points['intensity'].astype('f4')
        intensity = numpy.where(numpy.isfinite(intensity), intensity, 0.0)
        span = float(intensity.max() - intensity.min()) or 1.0
        level = ((intensity - intensity.min()) / span).reshape(-1, 1)
        block = numpy.hstack([xyz, level, level, level])
    else:
        block, colour_source = xyz, None
    stride = block.shape[1] if len(block) else 3
    return (dict(header_fields(message), binary='points', count=int(len(block)), stride=int(stride),
                 total_points=int(total), valid_points=int(valid), colour=colour_source),
            numpy.ascontiguousarray(block, dtype='<f4').tobytes())


def encode_laser_scan(message, max_points=MAX_SCAN_POINTS):
    """Describe a planar laser scan as angles and ranges for a top-down drawing.

    Very dense scans keep every n-th ray. Ranges that are not finite, such as
    'no return', are sent as null so the viewer can skip them. The angle of
    ray ``i`` is ``angle_min + i * angle_increment``, where the increment
    already includes the thinning.
    """
    count = len(message.ranges)
    step = max(1, math.ceil(count / max_points))
    return dict(header_fields(message), angle_min=message.angle_min,
                angle_increment=message.angle_increment * step, range_min=message.range_min,
                range_max=message.range_max, total_rays=count,
                ranges=numbers(message.ranges[::step])), None


def vector(value):
    """Describe a three-component vector with finite numbers or nulls."""
    return dict(x=finite(value.x), y=finite(value.y), z=finite(value.z))


def encode_imu(message):
    """Describe an inertial measurement: orientation, turn rate and acceleration.

    By ROS convention a first covariance element of -1 means that the sensor
    does not provide that quantity. The header says for each of the three
    quantities whether it is provided, so the viewer does not draw a
    meaningless orientation.
    """
    quaternion = message.orientation
    norm = math.sqrt(quaternion.x ** 2 + quaternion.y ** 2 + quaternion.z ** 2 + quaternion.w ** 2)
    return dict(
        header_fields(message),
        orientation=dict(x=finite(quaternion.x), y=finite(quaternion.y), z=finite(quaternion.z),
                         w=finite(quaternion.w)),
        has_orientation=bool(message.orientation_covariance[0] != -1 and math.isfinite(norm) and norm > 1e-6),
        angular_velocity=vector(message.angular_velocity),
        has_angular_velocity=bool(message.angular_velocity_covariance[0] != -1),
        linear_acceleration=vector(message.linear_acceleration),
        has_linear_acceleration=bool(message.linear_acceleration_covariance[0] != -1)), None


def encode_joint_state(message):
    """List joint names with whichever of position, velocity and effort are present.

    The three arrays may each be empty, meaning 'not reported'; they are
    sent as they are and the viewer shows a dash for missing values.
    """
    return dict(header_fields(message), names=list(message.name), position=numbers(message.position),
                velocity=numbers(message.velocity), effort=numbers(message.effort)), None


def encode_nav_sat_fix(message):
    """Describe a satellite position fix and whether it is usable.

    A fix is valid when the receiver reports a fix status of zero or more
    and latitude and longitude are finite. The horizontal accuracy is the
    square root of the larger horizontal variance, when the covariance is known.
    """
    latitude, longitude = finite(message.latitude), finite(message.longitude)
    accuracy = None
    if message.position_covariance_type != 0:
        variance = max(message.position_covariance[0], message.position_covariance[4])
        accuracy = float(math.sqrt(variance)) if math.isfinite(variance) and variance >= 0 else None
    return dict(header_fields(message), latitude=latitude, longitude=longitude,
                altitude=finite(message.altitude), status=int(message.status.status),
                service=int(message.status.service), horizontal_accuracy_m=accuracy,
                valid=bool(message.status.status >= 0 and latitude is not None and longitude is not None)), None


def encode_range(message):
    """Describe a single distance reading with the sensor's limits and beam width."""
    return dict(header_fields(message), range=finite(message.range), min_range=finite(message.min_range),
                max_range=finite(message.max_range), field_of_view=finite(message.field_of_view),
                radiation_type={0: 'ultrasound', 1: 'infrared'}.get(message.radiation_type, 'unknown')), None


def encode_camera_info(message):
    """Describe a camera calibration: image size, distortion and projection matrices.

    The matrices are sent row by row exactly as in the message: K (3x3
    intrinsics), R (3x3 rectification) and P (3x4 projection), plus the
    distortion coefficients D, binning and region of interest.
    """
    roi = message.roi
    return dict(header_fields(message), width=message.width, height=message.height,
                distortion_model=message.distortion_model, d=numbers(message.d), k=numbers(message.k),
                r=numbers(message.r), p=numbers(message.p), binning_x=message.binning_x,
                binning_y=message.binning_y,
                roi=dict(x_offset=roi.x_offset, y_offset=roi.y_offset, width=roi.width,
                         height=roi.height, do_rectify=bool(roi.do_rectify))), None


REGISTRY = {
    'sensor_msgs/msg/Image': dict(viewer='image', label='Camera image', rate=10, encode=encode_image),
    'sensor_msgs/msg/CompressedImage': dict(viewer='image', label='Compressed image', rate=10,
                                            encode=encode_compressed_image),
    'sensor_msgs/msg/PointCloud2': dict(viewer='pointcloud', label='Point cloud', rate=5,
                                        encode=encode_point_cloud),
    'sensor_msgs/msg/LaserScan': dict(viewer='laserscan', label='Laser scan', rate=10, encode=encode_laser_scan),
    'sensor_msgs/msg/Imu': dict(viewer='imu', label='IMU', rate=20, encode=encode_imu),
    'sensor_msgs/msg/JointState': dict(viewer='jointstate', label='Joint states', rate=10,
                                       encode=encode_joint_state),
    'sensor_msgs/msg/NavSatFix': dict(viewer='navsatfix', label='Satellite position', rate=5,
                                      encode=encode_nav_sat_fix),
    'sensor_msgs/msg/Range': dict(viewer='range', label='Range', rate=10, encode=encode_range),
    'sensor_msgs/msg/CameraInfo': dict(viewer='camerainfo', label='Camera calibration', rate=2,
                                       encode=encode_camera_info),
}


def registry_description():
    """List the supported sensor types with their viewer and default rate."""
    return [dict(type=type_name, viewer=entry['viewer'], label=entry['label'], default_rate=entry['rate'])
            for type_name, entry in REGISTRY.items()]


def sensor_topics(graph):
    """Pick the topics from a graph description that a sensor viewer can show.

    A topic qualifies when one of its message types is in the registry.
    Hidden topics are left out. Each result names the viewer to use and how
    many publishers currently exist, so a view can show idle topics as such.
    """
    found = []
    for topic in graph['topics']:
        if topic['hidden']:
            continue
        for type_name in topic['types']:
            entry = REGISTRY.get(type_name)
            if entry is not None:
                found.append(dict(topic=topic['name'], type=type_name, viewer=entry['viewer'],
                                  label=entry['label'], publisher_count=topic['publisher_count']))
    return found
