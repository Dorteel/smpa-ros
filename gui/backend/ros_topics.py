"""Describe topics and convert arbitrary ROS messages to bounded JSON.

Concepts:
    Plain data is what JSON can carry: dictionaries, lists, strings, finite
    numbers, booleans and null. A ROS message is converted field by field.
    Long arrays and strings are shortened, because a camera image or point
    cloud would otherwise send megabytes of numbers to a text view.
"""
import math

from backend.gateway import GatewayError

ARRAY_LIMIT = 64
STRING_LIMIT = 2000


def message_class(type_name):
    """Find the Python class of a message type such as ``std_msgs/msg/String``.

    The class comes from the ROS environment this process was started in. A
    type from a workspace that was not sourced cannot be found; that is
    reported as a ``GatewayError`` naming the type, so a view can explain it.
    """
    from rosidl_runtime_py.utilities import get_message
    try:
        return get_message(type_name)
    except (AttributeError, ModuleNotFoundError, ValueError) as error:
        raise GatewayError(f'Message type {type_name} is not available in this ROS environment '
                           f'(source the workspace that defines it): {error}') from error


def subscription_qos(node, topic):
    """Choose a subscription quality of service that matches the current publishers.

    A best-effort, volatile subscription can listen to every publisher, so
    that is the default. Reliability is raised to reliable only when every
    publisher is reliable. Durability is raised to transient-local only when
    every publisher offers it, which lets latched topics deliver their last
    message. Only the newest message is kept in the queue.
    """
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    publishers = node.get_publishers_info_by_topic(topic)
    reliable = bool(publishers) and all(
        info.qos_profile.reliability == ReliabilityPolicy.RELIABLE for info in publishers)
    latched = bool(publishers) and all(
        info.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for info in publishers)
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST, depth=1,
        reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)


def plain(value, state=None):
    """Convert a ROS message or field value to bounded plain data.

    Messages become dictionaries of their fields. Sequences longer than the
    array limit keep only their first elements and report their full length.
    Byte arrays are summarised by length. Long strings are cut. Numbers that
    JSON cannot express (NaN and infinities) become strings. ``state`` is a
    dictionary whose ``truncated`` entry is set when anything was shortened.
    """
    state = state if state is not None else {}
    if hasattr(value, 'get_fields_and_field_types'):
        return {name: plain(getattr(value, name), state) for name in value.get_fields_and_field_types()}
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        state['truncated'] = True
        return {'__bytes__': len(value)}
    if isinstance(value, str):
        if len(value) > STRING_LIMIT:
            state['truncated'] = True
            return value[:STRING_LIMIT] + '…'
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, int):
        return value
    if hasattr(value, 'tolist') and not hasattr(value, '__len__'):
        return plain(value.tolist(), state)  # a numpy scalar
    if hasattr(value, '__len__'):
        length = len(value)
        head = value[:ARRAY_LIMIT]
        head = head.tolist() if hasattr(head, 'tolist') else list(head)
        items = [plain(item, state) for item in head]
        if length > ARRAY_LIMIT:
            state['truncated'] = True
            return {'__truncated__': True, 'length': length, 'head': items}
        return items
    return str(value)


def topic_info(graph, name):
    """Extract one topic's types, publishers and subscribers from a graph description.

    Publishers and subscribers are listed with their node, message type and
    quality of service. Return ``None`` when the topic is not currently
    known, which is an ordinary situation and not an error.
    """
    topic = next((item for item in graph['topics'] if item['name'] == name), None)
    if topic is None:
        return None
    node_names = {node['id']: node['full_name'] for node in graph['nodes']}
    publishers, subscribers = [], []
    for edge in graph['edges']:
        if edge['kind'] == 'publishes' and edge['target'] == topic['id']:
            publishers.append(dict(node=node_names[edge['source']], type=edge['type'], qos=edge['qos']))
        elif edge['kind'] == 'subscribes' and edge['source'] == topic['id']:
            subscribers.append(dict(node=node_names[edge['target']], type=edge['type'], qos=edge['qos']))
    return dict(topic, publishers=publishers, subscribers=subscribers)
