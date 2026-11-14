"""Tests of the pure conversion logic; these need NumPy and Pillow but no ROS."""
import io
import math
import struct
from types import SimpleNamespace as NS

import pytest

from backend import ros_graph, ros_names, ros_topics, sensors


def header(frame='frame'):
    """Build a stand-in for a ROS message header."""
    return NS(frame_id=frame, stamp=NS(sec=10, nanosec=500000000))


def qos():
    """Build a plain QoS description as the graph collector would produce it."""
    return dict(reliability='reliable', durability='volatile', history='keep_last', depth=10,
                liveliness='automatic', deadline_s=None, lifespan_s=None)


def raw_graph():
    """Describe a small ROS graph: talker to listener, one action, and the gateway."""
    endpoint = lambda node, kind='std_msgs/msg/String': dict(node=node, type=kind, qos=qos())
    status = 'action_msgs/msg/GoalStatusArray'
    return dict(
        nodes=[dict(name='talker', namespace='/'), dict(name='listener', namespace='/robot'),
               dict(name='smpa_gui', namespace='/')],
        topics=[dict(name='/chatter', types=['std_msgs/msg/String'], publishers=[endpoint('/talker')],
                     subscribers=[endpoint('/robot/listener')]),
                dict(name='/rosout', types=['rcl_interfaces/msg/Log'], publishers=[endpoint('/talker')],
                     subscribers=[]),
                dict(name='/fib/_action/status', types=[status], publishers=[endpoint('/server', status)],
                     subscribers=[endpoint('/talker', status)])],
        services=[dict(name='/fib/_action/send_goal', types=['x/action/Fib_SendGoal'])],
        actions=[dict(name='/fib', types=['x/action/Fib'])])


def test_names():
    """Names are joined without doubled slashes and hidden parts are recognised."""
    assert ros_names.full_node_name('/', 'a') == '/a'
    assert ros_names.full_node_name('/ns', 'a') == '/ns/a'
    assert ros_names.is_hidden('/a/_b/c') and not ros_names.is_hidden('/a/b_c')
    assert ros_names.action_of('/fib/_action/status') == '/fib'
    assert ros_names.action_of('/chatter') is None


def test_graph_description():
    """Raw discovery becomes nodes, flagged topics, directed edges and actions."""
    graph = ros_graph.build(raw_graph(), gateway_name='/smpa_gui')
    nodes = {node['full_name']: node for node in graph['nodes']}
    assert nodes['/smpa_gui']['is_gateway'] and not nodes['/talker']['is_gateway']
    assert '/server' in nodes, 'a node known only from an endpoint is still listed'
    topics = {topic['name']: topic for topic in graph['topics']}
    assert topics['/rosout']['infrastructure'] and not topics['/chatter']['infrastructure']
    assert topics['/fib/_action/status']['hidden'] and topics['/fib/_action/status']['action'] == '/fib'
    publish = next(edge for edge in graph['edges'] if edge['kind'] == 'publishes' and edge['target'] == 'topic:/chatter')
    subscribe = next(edge for edge in graph['edges'] if edge['kind'] == 'subscribes' and edge['source'] == 'topic:/chatter')
    assert publish['source'] == 'node:/talker' and subscribe['target'] == 'node:/robot/listener'
    assert graph['actions'] == [dict(id='action:/fib', name='/fib', types=['x/action/Fib'],
                                     servers=['/server'], clients=['/talker'])]
    info = ros_topics.topic_info(graph, '/chatter')
    assert [item['node'] for item in info['publishers']] == ['/talker']
    assert ros_topics.topic_info(graph, '/missing') is None


class FakeMessage:
    """Imitate a generated ROS message with named fields."""

    def __init__(self, **fields):
        """Store the fields as attributes."""
        self.__dict__.update(fields)

    def get_fields_and_field_types(self):
        """Report field names like a generated message class does."""
        return {name: 'unknown' for name in self.__dict__}


def test_plain_conversion_bounds_large_and_special_values():
    """Messages become JSON-safe data; long arrays, bytes and non-finite numbers are handled."""
    state = {}
    message = FakeMessage(text='hi', number=float('nan'), inner=FakeMessage(values=list(range(200))),
                          blob=b'\x00' * 10, small=[1, 2])
    result = ros_topics.plain(message, state)
    assert result['text'] == 'hi' and result['number'] == 'nan' and result['small'] == [1, 2]
    assert result['inner']['values'] == {'__truncated__': True, 'length': 200, 'head': list(range(64))}
    assert result['blob'] == {'__bytes__': 10} and state['truncated']
    import json
    json.dumps(result, allow_nan=False)


def image(encoding, width, height, data, step=None, bigendian=0):
    """Build a stand-in for sensor_msgs/Image."""
    return NS(header=header(), encoding=encoding, width=width, height=height,
              step=step or len(data) // height, data=data, is_bigendian=bigendian)


def decode(jpeg):
    """Open JPEG bytes as an RGB picture."""
    from PIL import Image
    return Image.open(io.BytesIO(jpeg)).convert('RGB')


def test_image_colour_order_and_padding():
    """rgb8 and bgr8 give the same picture, and padded rows are skipped."""
    red_rgb = image('rgb8', 8, 8, bytes([255, 0, 0] * 64))
    red_bgr = image('bgr8', 8, 8, bytes([0, 0, 255] * 64))
    padded = image('rgb8', 8, 8, (bytes([255, 0, 0] * 8) + b'\x07' * 5) * 8, step=29)
    for message in (red_rgb, red_bgr, padded):
        described, jpeg = sensors.encode_image(message)
        r, g, b = decode(jpeg).getpixel((4, 4))
        assert r > 200 and g < 60 and b < 60
        assert described['binary'] == 'jpeg' and described['width'] == 8 and not described['scaled']


def test_depth_image_is_scaled_and_wide_image_is_shrunk():
    """A 16-bit image is contrast-stretched; a wide image is reduced to the width limit."""
    ramp = image('16UC1', 16, 2, struct.pack('<32H', *range(32)))
    described, jpeg = sensors.encode_image(ramp)
    picture = decode(jpeg)
    assert described['scaled'] and picture.getpixel((0, 0))[0] < picture.getpixel((15, 1))[0]
    wide = image('mono8', 400, 2, bytes(800))
    assert decode(sensors.encode_image(wide, max_width=100)[1]).width == 100


def test_unsupported_and_inconsistent_images_are_refused():
    """Unknown encodings and wrong sizes raise UnsupportedData instead of crashing."""
    with pytest.raises(sensors.UnsupportedData, match='bayer'):
        sensors.encode_image(image('bayer_rggb8', 2, 2, bytes(4)))
    with pytest.raises(sensors.UnsupportedData):
        sensors.encode_image(image('rgb8', 4, 4, bytes(10), step=12))


def test_compressed_image_passthrough_and_reencode():
    """JPEG data is forwarded unchanged, PNG is re-encoded, garbage is refused."""
    from PIL import Image
    buffers = {}
    for kind in ('JPEG', 'PNG'):
        buffer = io.BytesIO()
        Image.new('RGB', (10, 6), (0, 0, 255)).save(buffer, format=kind)
        buffers[kind] = buffer.getvalue()
    jpeg = NS(header=header(), format='jpeg', data=buffers['JPEG'])
    assert sensors.encode_compressed_image(jpeg)[1] == buffers['JPEG']
    described, data = sensors.encode_compressed_image(NS(header=header(), format='png', data=buffers['PNG']))
    assert data[:2] == b'\xff\xd8' and described['width'] == 10
    with pytest.raises(sensors.UnsupportedData):
        sensors.encode_compressed_image(NS(header=header(), format='16UC1; compressedDepth', data=b'nonsense'))


def cloud(points, fields, step, bigendian=False):
    """Build a stand-in for sensor_msgs/PointCloud2 from packed point bytes."""
    return NS(header=header(), fields=[NS(name=name, offset=offset, datatype=datatype, count=1)
                                       for name, offset, datatype in fields],
              point_step=step, width=len(points) // step, height=1, is_bigendian=bigendian, data=points)


def test_point_cloud_decoding_colour_and_limits():
    """Positions and packed colours are read, invalid points dropped, large clouds thinned."""
    import numpy
    fields = [('x', 0, 7), ('y', 4, 7), ('z', 8, 7), ('rgb', 12, 6)]
    data = struct.pack('<fffI', 1.0, 2.0, 3.0, 0xFF0000) + struct.pack('<fffI', math.nan, 0, 0, 0) \
        + struct.pack('<fffI', 4.0, 5.0, 6.0, 0x0000FF)
    described, block = sensors.encode_point_cloud(cloud(data, fields, 16))
    values = numpy.frombuffer(block, dtype='<f4').reshape(-1, 6)
    assert described['count'] == 2 and described['stride'] == 6 and described['total_points'] == 3
    assert values[0].tolist() == [1.0, 2.0, 3.0, 1.0, 0.0, 0.0] and values[1].tolist() == [4.0, 5.0, 6.0, 0.0, 0.0, 1.0]
    plain_fields = [('x', 0, 7), ('y', 4, 7), ('z', 8, 7)]
    many = b''.join(struct.pack('<fff', i, 0, 0) for i in range(1000))
    described, block = sensors.encode_point_cloud(cloud(many, plain_fields, 12), max_points=100)
    assert described['stride'] == 3 and described['count'] == 100 and len(block) == 100 * 12
    assert described['colour'] is None and described['valid_points'] == 1000
    with pytest.raises(sensors.UnsupportedData):
        sensors.encode_point_cloud(cloud(b'', [('x', 0, 7)], 4))


def test_scalar_sensor_encoders():
    """Scan, IMU, joints, fix, range and calibration become JSON-safe descriptions."""
    import json
    scan = NS(header=header(), angle_min=-1.0, angle_increment=0.001, range_min=0.1, range_max=5.0,
              ranges=[1.0, 2.0, math.inf, math.nan] * 750)
    described = sensors.encode_laser_scan(scan)[0]
    assert described['total_rays'] == 3000 and len(described['ranges']) <= sensors.MAX_SCAN_POINTS
    assert described['angle_increment'] == pytest.approx(0.003) and None in described['ranges']
    vector = lambda x=0.0, y=0.0, z=0.0: NS(x=x, y=y, z=z)
    imu = NS(header=header(), orientation=NS(x=0.0, y=0.0, z=0.0, w=1.0), orientation_covariance=[-1.0] + [0.0] * 8,
             angular_velocity=vector(z=0.5), angular_velocity_covariance=[0.0] * 9,
             linear_acceleration=vector(z=9.81), linear_acceleration_covariance=[0.0] * 9)
    described_imu = sensors.encode_imu(imu)[0]
    assert not described_imu['has_orientation'] and described_imu['has_angular_velocity']
    joints = sensors.encode_joint_state(NS(header=header(), name=['a'], position=[math.nan], velocity=[], effort=[]))[0]
    assert joints['position'] == [None] and joints['velocity'] == []
    fix = NS(header=header(), latitude=52.0, longitude=4.0, altitude=math.nan, status=NS(status=-1, service=1),
             position_covariance=[4.0, 0, 0, 0, 9.0, 0, 0, 0, 1.0], position_covariance_type=2)
    described_fix = sensors.encode_nav_sat_fix(fix)[0]
    assert not described_fix['valid'] and described_fix['horizontal_accuracy_m'] == 3.0
    reading = sensors.encode_range(NS(header=header(), range=math.inf, min_range=0.1, max_range=2.0,
                                      field_of_view=0.2, radiation_type=1))[0]
    assert reading['range'] is None and reading['radiation_type'] == 'infrared'
    info = NS(header=header(), width=640, height=480, distortion_model='plumb_bob', d=[0.0] * 5, k=[1.0] * 9,
              r=[1.0] * 9, p=[1.0] * 12, binning_x=0, binning_y=0,
              roi=NS(x_offset=0, y_offset=0, width=0, height=0, do_rectify=False))
    for item in (described, described_imu, joints, described_fix, reading, sensors.encode_camera_info(info)[0]):
        json.dumps(item, allow_nan=False)


def test_registry_covers_the_required_types_and_filters_topics():
    """All nine required sensor types are registered; other and hidden topics are ignored."""
    required = {'Image', 'CompressedImage', 'PointCloud2', 'LaserScan', 'Imu', 'JointState', 'NavSatFix',
                'Range', 'CameraInfo'}
    assert {name.rsplit('/', 1)[1] for name in sensors.REGISTRY} == required
    graph = dict(topics=[
        dict(name='/scan', types=['sensor_msgs/msg/LaserScan'], hidden=False, publisher_count=0),
        dict(name='/_hidden/scan', types=['sensor_msgs/msg/LaserScan'], hidden=True, publisher_count=1),
        dict(name='/chatter', types=['std_msgs/msg/String'], hidden=False, publisher_count=1)])
    assert sensors.sensor_topics(graph) == [dict(topic='/scan', type='sensor_msgs/msg/LaserScan',
                                                 viewer='laserscan', label='Laser scan', publisher_count=0)]
