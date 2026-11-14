"""Read another node's parameters through its standard parameter services.

Concepts:
    Every ROS 2 node normally offers ``list_parameters``, ``get_parameters``
    and ``describe_parameters`` services. A node answers them only while it
    is spinning. A node that is busy (for example during model inference) or
    that never spins does not answer; that is reported as the state
    ``not_responding`` rather than as a failure. Nothing is ever written.
"""
import threading

PARAMETER_TYPES = {0: 'not_set', 1: 'bool', 2: 'integer', 3: 'double', 4: 'string', 5: 'byte_array',
                   6: 'bool_array', 7: 'integer_array', 8: 'double_array', 9: 'string_array'}
VALUE_FIELDS = {1: 'bool_value', 2: 'integer_value', 3: 'double_value', 4: 'string_value',
                5: 'byte_array_value', 6: 'bool_array_value', 7: 'integer_array_value',
                8: 'double_array_value', 9: 'string_array_value'}


def parameter_value(value):
    """Extract the Python value held by a ``ParameterValue`` message.

    The message has one field per possible type and a type number saying
    which one is meaningful. Arrays become lists and byte arrays become lists
    of integers so the result is plain data. An unset parameter gives ``None``.
    """
    field = VALUE_FIELDS.get(value.type)
    if field is None:
        return None
    content = getattr(value, field)
    if value.type == 5:
        return [int(byte[0]) if isinstance(byte, bytes) else int(byte) for byte in content]
    if value.type >= 6:
        return list(content)
    return content


def request(gateway, service_type, service_name, message, timeout):
    """Call one service on another node and wait a bounded time for the answer.

    Create a client on the ROS thread and send the request only if the
    service currently exists. Wait for the reply on the calling thread, then
    always destroy the client. Return ``('ok', response)``, or
    ``('unavailable', None)`` when the service does not exist, or
    ``('not_responding', None)`` when no reply arrived in time.
    """
    answered = threading.Event()
    holder = {}

    def send():
        """Create the client and, if the service exists, send the request."""
        holder['client'] = gateway.node.create_client(service_type, service_name)
        if not holder['client'].service_is_ready():
            return False
        holder['future'] = holder['client'].call_async(message)
        holder['future'].add_done_callback(lambda future: answered.set())
        return True

    try:
        if not gateway.call(send):
            return 'unavailable', None
        if not answered.wait(timeout):
            return 'not_responding', None
        return 'ok', holder['future'].result()
    finally:
        if 'client' in holder:
            gateway.call(lambda: gateway.node.destroy_client(holder['client']))


def read(gateway, node_name, timeout=2.0):
    """List a node's parameters with their types, values and descriptions.

    Ask the node for its parameter names, then for the values and the
    descriptors of those names. If the node offers no parameter services the
    state is ``unavailable``; if it does not answer in time the state is
    ``not_responding``. In both cases the parameter list is empty and the
    caller can show the state. Descriptions are optional: when only that
    request fails, values are still returned.
    """
    from rcl_interfaces.srv import DescribeParameters, GetParameters, ListParameters

    prefix = node_name.rstrip('/')
    state, listed = request(gateway, ListParameters, prefix + '/list_parameters',
                            ListParameters.Request(), timeout)
    if state != 'ok':
        return dict(node=node_name, state=state, parameters=[])
    names = sorted(listed.result.names)
    if not names:
        return dict(node=node_name, state='ok', parameters=[])
    state, values = request(gateway, GetParameters, prefix + '/get_parameters',
                            GetParameters.Request(names=names), timeout)
    if state != 'ok':
        return dict(node=node_name, state=state, parameters=[])
    _, described = request(gateway, DescribeParameters, prefix + '/describe_parameters',
                           DescribeParameters.Request(names=names), timeout)
    descriptors = list(described.descriptors) if described is not None else []
    parameters = []
    for index, (name, value) in enumerate(zip(names, values.values)):
        descriptor = descriptors[index] if index < len(descriptors) else None
        parameters.append(dict(
            name=name, type=PARAMETER_TYPES.get(value.type, 'unknown'), value=parameter_value(value),
            description=descriptor.description if descriptor else '',
            read_only=bool(descriptor.read_only) if descriptor else None))
    return dict(node=node_name, state='ok', parameters=parameters)
