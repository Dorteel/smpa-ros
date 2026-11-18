"""Read and, on explicit request, change another node's parameters through its standard services.

Concepts:
    Every ROS 2 node normally offers ``list_parameters``, ``get_parameters``,
    ``describe_parameters``, ``set_parameters`` and
    ``set_parameters_atomically`` services. A node answers them only while it
    is spinning. A node that is busy (for example during model inference) or
    that never spins does not answer; that is reported as the state
    ``not_responding`` rather than as a failure.

    A descriptor is what the node says about a parameter: its declared type,
    a description, whether it is read-only, whether its type may change
    (dynamic typing), free-text constraints, and an optional integer or
    floating-point range with a step.

    A change is one proposed new value (and type) for one parameter. Changes
    are checked here against the node's current descriptors before anything
    is sent; the node then has the final word and may reject a change with a
    reason. Several changes are applied either atomically (all or nothing,
    one answer) or individually (one answer per parameter, so some may
    succeed while others are rejected). Nothing is ever written without an
    explicit request from the web layer, and a request is never retried.
"""
import math
import threading
import time

TYPE_NUMBERS = {'bool': 1, 'integer': 2, 'double': 3, 'string': 4, 'byte_array': 5, 'bool_array': 6,
                'integer_array': 7, 'double_array': 8, 'string_array': 9}
INTEGER_LIMITS = (-2 ** 63, 2 ** 63 - 1)
CHANGES_KEPT = 200
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

    Create a client on the ROS thread. A new client needs a moment to match
    the service it names, so wait up to one second (or the timeout, if
    shorter) for the service to be reported as available; send the request
    only then. Wait for the reply on the calling thread, then always destroy
    the client. Return ``('ok', response)``, or ``('unavailable', None)``
    when the service does not exist, or ``('not_responding', None)`` when no
    reply arrived in time.
    """
    answered = threading.Event()
    holder = {}

    def create():
        """Create the client on the ROS thread."""
        holder['client'] = gateway.node.create_client(service_type, service_name)

    def send():
        """Send the request and arrange to be told about the answer."""
        holder['future'] = holder['client'].call_async(message)
        holder['future'].add_done_callback(lambda future: answered.set())

    try:
        gateway.call(create)
        deadline = time.monotonic() + min(1.0, timeout)
        while not gateway.call(holder['client'].service_is_ready):
            if time.monotonic() > deadline:
                return 'unavailable', None
            time.sleep(0.05)
        gateway.call(send)
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
        parameters.append(dict(name=name, type=PARAMETER_TYPES.get(value.type, 'unknown'), value=parameter_value(value),
                               **describe_descriptor(descriptor)))
    return dict(node=node_name, state='ok', parameters=parameters)


def read_many(gateway, node_names, timeout=1.0, readers=8):
    """Read the parameters of several nodes at once, each node with the same rules as ``read``.

    This is what the Workspace's parameter panel shows: every discovered
    node with its parameters, so that each parameter stays attached to the
    node that owns it. A node that is busy or never spins would make a
    one-by-one reading as slow as the sum of all timeouts, so the nodes are
    read side by side by a small, fixed number of reader threads. Each
    reader calls ``read`` for one node; nothing is written. A node whose
    reading fails because the gateway stopped is reported with the state
    ``error`` and the reason, instead of hiding the other nodes. The result
    keeps the order of ``node_names``.
    """
    from concurrent.futures import ThreadPoolExecutor

    def read_one(node_name):
        """Read one node, turning an unexpected failure into a state of that node."""
        try:
            return read(gateway, node_name, timeout)
        except Exception as error:  # noqa: BLE001 - one node's failure must not hide the others
            return dict(node=node_name, state='error', detail=str(error), parameters=[])

    if not node_names:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(readers, len(node_names)))) as pool:
        return list(pool.map(read_one, node_names))


def describe_descriptor(descriptor):
    """Turn a ``ParameterDescriptor`` into plain data, or into unknowns when the node gave none.

    The description, the read-only flag, dynamic typing, the free-text
    additional constraints and the declared type are copied. A range is a
    list with at most one entry in ROS; it is reported as ``{from, to,
    step}`` or ``None``. ``descriptor_known`` says whether the node answered
    the describe request at all, because without it read-only and ranges
    cannot be checked here (the node still checks them itself).
    """
    if descriptor is None:
        return dict(description='', read_only=None, dynamic_typing=None, additional_constraints='',
                    declared_type=None, integer_range=None, floating_point_range=None, descriptor_known=False)
    integer = descriptor.integer_range[0] if len(descriptor.integer_range) else None
    floating = descriptor.floating_point_range[0] if len(descriptor.floating_point_range) else None
    return dict(description=descriptor.description, read_only=bool(descriptor.read_only),
                dynamic_typing=bool(descriptor.dynamic_typing),
                additional_constraints=descriptor.additional_constraints,
                declared_type=PARAMETER_TYPES.get(descriptor.type, 'unknown'),
                integer_range=dict(**{'from': integer.from_value, 'to': integer.to_value, 'step': integer.step})
                if integer else None,
                floating_point_range=dict(**{'from': floating.from_value, 'to': floating.to_value, 'step': floating.step})
                if floating else None,
                descriptor_known=True)


def in_range(value, bounds, integer):
    """Tell whether a number lies within a ROS parameter range and on its step grid.

    ROS ranges include both ends. A step of zero means any value in the
    range. Otherwise the value must be a whole number of steps away from
    the lower end, or equal the upper end (ROS allows the upper end even
    when it is off the grid). Floating-point steps are compared with a
    small tolerance, as rclpy does.
    """
    low, high, step = bounds['from'], bounds['to'], bounds['step']
    if not low <= value <= high:
        return False
    if not step or value == high:
        return True
    if integer:
        return (value - low) % step == 0
    distance = (value - low) / step
    return math.isclose(distance, round(distance), abs_tol=1e-9)


def check_scalar(kind, value, path, errors):
    """Check one scalar against a parameter element type and record a problem if it does not fit.

    Booleans must be true or false. Integers must be whole numbers within
    the 64-bit range (not booleans). Doubles must be finite numbers. Strings
    must be text. Bytes must be whole numbers from 0 to 255.
    """
    if kind == 'bool' and not isinstance(value, bool):
        errors.append(dict(path=path, message='must be true or false'))
    elif kind in ('integer', 'byte'):
        low, high = (0, 255) if kind == 'byte' else INTEGER_LIMITS
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(dict(path=path, message='must be a whole number'))
        elif not low <= value <= high:
            errors.append(dict(path=path, message=f'must be between {low} and {high}'))
    elif kind == 'double':
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            errors.append(dict(path=path, message='must be a finite number'))
    elif kind == 'string' and not isinstance(value, str):
        errors.append(dict(path=path, message='must be text'))


def check_change(current, change):
    """Check one proposed change against what the node said about the parameter; return the problems.

    ``current`` is the parameter as read from the node (with its
    descriptor), or ``None`` when the node does not have it. ``change`` has
    the parameter ``name``, the proposed ``type`` and the proposed
    ``value``. The rules, in order: the parameter must exist (ROS nodes
    normally refuse undeclared parameters); it must not be read-only; the
    type must be a settable type and may differ from the current type only
    with dynamic typing; the value must fit the type, element by element for
    arrays; and an integer or floating-point range, when declared, must be
    respected. An empty list means the change may be sent; the node may
    still reject it.
    """
    errors = []
    name, kind, value = change['name'], change['type'], change['value']
    if current is None:
        return [dict(path=name, message='is not a parameter of this node (it may have been undeclared)')]
    if current.get('read_only'):
        return [dict(path=name, message='is read-only; the node does not allow changing it')]
    if kind not in TYPE_NUMBERS:
        return [dict(path=name, message=f'has unsupported type {kind!r}')]
    if kind != current['type'] and current['type'] != 'not_set' and current.get('dynamic_typing') is not True:
        errors.append(dict(path=name, message=f"must stay of type {current['type']} (the node does not allow "
                                              'changing the type)'))
    if kind.endswith('_array'):
        element = {'byte_array': 'byte'}.get(kind, kind[:-len('_array')])
        if not isinstance(value, list):
            errors.append(dict(path=name, message='must be a list'))
        else:
            for index, item in enumerate(value):
                check_scalar(element, item, f'{name}[{index}]', errors)
    else:
        check_scalar(kind, value, name, errors)
    if errors:
        return errors
    integer_range, floating_range = current.get('integer_range'), current.get('floating_point_range')
    if kind == 'integer' and integer_range and not in_range(value, integer_range, True):
        errors.append(dict(path=name, message=f"must be within {integer_range['from']}…{integer_range['to']}"
                                              + (f" in steps of {integer_range['step']}" if integer_range['step'] else '')))
    if kind == 'double' and floating_range and not in_range(float(value), floating_range, False):
        errors.append(dict(path=name, message=f"must be within {floating_range['from']}…{floating_range['to']}"
                                              + (f" in steps of {floating_range['step']}" if floating_range['step'] else '')))
    return errors


def parameter_message(change):
    """Build the ``rcl_interfaces/msg/Parameter`` that carries one checked change."""
    from rcl_interfaces.msg import Parameter, ParameterValue
    value = ParameterValue(type=TYPE_NUMBERS[change['type']])
    content = change['value']
    if change['type'] == 'double':
        content = float(content)
    elif change['type'] == 'double_array':
        content = [float(item) for item in content]
    elif change['type'] == 'byte_array':
        content = [bytes([item]) for item in content]
    setattr(value, VALUE_FIELDS[value.type], content)
    return Parameter(name=change['name'], value=value)


class ParameterChangeRefused(Exception):
    """A change request was not sent; ``status`` is the HTTP status and ``errors`` lists the problems."""

    def __init__(self, message, status, errors=None):
        """Remember the explanation, the HTTP status and the per-parameter problems."""
        super().__init__(message)
        self.status = status
        self.errors = errors or []


class ParameterWriter:
    """Apply confirmed parameter changes once each and remember what happened in this session.

    A change record holds the node, the requested changes with the values
    they replaced, whether they were applied atomically, the outcome and the
    node's answer per parameter, and the values read back afterwards.
    Records live only in this GUI process and are kept until it stops.
    """

    def __init__(self, gateway):
        """Start with no records for a running gateway."""
        self.gateway = gateway
        self.lock = threading.Lock()
        self.records = []
        self.requests = {}

    def list(self):
        """Return the change records of this session, newest first."""
        with self.lock:
            return [dict(record) for record in reversed(self.records)]

    def apply(self, node_name, changes, atomic, request_id, timeout=3.0):
        """Check and send one confirmed set of parameter changes; a repeated request id sends nothing.

        First, a known request id returns the earlier record marked as a
        duplicate. Then the node's parameters and descriptors are read
        afresh, so the checks use the node's state at this moment; a node
        that does not answer refuses the request (nothing sent). Every change
        is checked; any problem refuses the whole request (nothing sent).
        Otherwise the changes are sent once, through ``set_parameters_atomically``
        when atomic, or ``set_parameters`` otherwise. The node's answers are
        recorded per parameter: ``applied`` when all succeeded, ``rejected``
        when none did, ``partial`` when only some did. If the node does not
        answer in time, the outcome is ``unknown``: the change may or may not
        have been applied, and it is not retried. Finally the values are read
        back, so the caller sees what the node holds now.
        """
        from rcl_interfaces.srv import SetParameters, SetParametersAtomically

        with self.lock:
            if request_id in self.requests:
                return dict(self.requests[request_id], duplicate=True)
        before = read(self.gateway, node_name, timeout)
        if before['state'] != 'ok':
            raise ParameterChangeRefused(f"The node {node_name} cannot be changed now: its parameters are "
                                         f"{before['state'].replace('_', ' ')}", 409)
        current = {item['name']: item for item in before['parameters']}
        errors = [problem for change in changes for problem in check_change(current.get(change['name']), change)]
        if errors:
            raise ParameterChangeRefused('Nothing was changed: the proposed values do not fit', 422, errors)
        messages = [parameter_message(change) for change in changes]
        prefix = node_name.rstrip('/')
        if atomic:
            state, answer = request(self.gateway, SetParametersAtomically, prefix + '/set_parameters_atomically',
                                    SetParametersAtomically.Request(parameters=messages), timeout)
            answers = [answer.result] * len(changes) if state == 'ok' else []
        else:
            state, answer = request(self.gateway, SetParameters, prefix + '/set_parameters',
                                    SetParameters.Request(parameters=messages), timeout)
            answers = list(answer.results) if state == 'ok' else []
        results = []
        for index, change in enumerate(changes):
            entry = dict(name=change['name'], type=change['type'], value=change['value'],
                         previous_type=current[change['name']]['type'], previous_value=current[change['name']]['value'])
            if index < len(answers):
                entry.update(successful=bool(answers[index].successful), reason=answers[index].reason)
            else:
                entry.update(successful=None, reason='no answer from the node')
            results.append(entry)
        if state == 'unavailable':
            outcome = 'unavailable'
        elif state != 'ok':
            outcome = 'unknown'
        else:
            succeeded = sum(1 for entry in results if entry['successful'])
            outcome = 'applied' if succeeded == len(results) else 'rejected' if succeeded == 0 else 'partial'
        after = read(self.gateway, node_name, timeout)
        record = dict(change_id=request_id, node=node_name, atomic=bool(atomic), outcome=outcome, results=results,
                      requested_at=time.time(), parameters=after['parameters'], read_back_state=after['state'])
        with self.lock:
            self.requests[request_id] = record
            self.records.append(record)
            del self.records[:-CHANGES_KEPT]
        return dict(record)
