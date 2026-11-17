"""Discover ROS 2 services and call one on explicit request, keeping a record of each call.

Concepts:
    A service is a named request/response interface offered by a node (its
    provider). Clients are nodes that hold a client for it. Discovery is
    generic: no service is known in advance, and request and response
    schemas are derived from the installed interface definitions.

    A service kind groups services for filtering: ``parameter`` (the
    standard parameter services every node offers), ``action`` (the hidden
    plumbing of actions), ``type_description`` (the type-description
    service), and ``other`` (everything an application offers).

    A call record is the gateway's memory of one confirmed call: the service,
    its type, the request, the state (``pending``, ``succeeded``,
    ``timed_out``, ``server_lost``, ``failed``), the response and timestamps.
    Records live in the gateway process only, until it stops. A request id is
    chosen by the client for each confirmed call; repeating it returns the
    first record and calls nothing, so a double click or a page reload
    cannot call a service twice. Calls are never retried: when no response
    arrives in time the outcome is unknown, because the provider may have
    acted anyway.
"""
import threading
import time
import uuid

from smpa_gui.ros_adapter import ros_interfaces
from smpa_gui.ros_adapter.gateway import GatewayError
from smpa_gui.ros_adapter.ros_names import action_of, full_node_name, is_hidden

PARAMETER_SERVICES = ('describe_parameters', 'get_parameter_types', 'get_parameters', 'list_parameters',
                      'set_parameters', 'set_parameters_atomically')
CALLS_KEPT = 200
READY_WAIT = 2.0


class ServiceCallRefused(GatewayError):
    """A call was not made; ``status`` is the HTTP status that explains why."""

    def __init__(self, message, status):
        """Remember the explanation and the HTTP status for the web layer."""
        super().__init__(message)
        self.status = status


def service_class(type_name):
    """Find the Python class of a service type such as ``std_srvs/srv/Trigger``.

    The class comes from the ROS environment this process was started in. A
    type whose package is not sourced cannot be used; that is reported as a
    ``GatewayError`` naming the type.
    """
    from rosidl_runtime_py.utilities import get_service
    try:
        return get_service(type_name)
    except (AttributeError, ModuleNotFoundError, ValueError) as error:
        raise GatewayError(f'Service type {type_name} is not available in this ROS environment '
                           f'(source the workspace that defines it): {error}') from error


def kind_of(name):
    """Classify a service name as parameter, action, type_description or other plumbing-free service."""
    last = name.rsplit('/', 1)[-1]
    if action_of(name):
        return 'action'
    if last in PARAMETER_SERVICES:
        return 'parameter'
    if last == 'get_type_description':
        return 'type_description'
    return 'other'


def collect(node):
    """Ask ROS which node provides and which node uses each service; must run on the ROS thread.

    For every discovered node, read its service servers and its service
    clients. Nodes that vanish between questions are skipped. Return plain
    data: the service names with their types, and for each name the sets of
    provider and client node names.
    """
    services = {name: list(types) for name, types in node.get_service_names_and_types()}
    providers, clients = {}, {}
    for name, namespace in node.get_node_names_and_namespaces():
        owner = full_node_name(namespace, name)
        try:
            for service, _ in node.get_service_names_and_types_by_node(name, namespace):
                providers.setdefault(service, set()).add(owner)
            for service, _ in node.get_client_names_and_types_by_node(name, namespace):
                clients.setdefault(service, set()).add(owner)
        except Exception:  # the node left the graph meanwhile
            continue
    return services, providers, clients


def discover(gateway):
    """Return every discovered service with its types, kind, providers and clients, sorted by name.

    Discovery runs on the ROS thread and is shared for one second between
    callers, like the graph snapshot. The GUI's own node is named like any
    other; it provides no services and holds a client only while a call of
    its own is in progress.
    """
    def produce():
        """Collect on the ROS thread and turn the result into sorted plain entries."""
        services, providers, clients = gateway.call(lambda: collect(gateway.node))
        return [dict(name=name, types=types, kind=kind_of(name), hidden=is_hidden(name),
                     providers=sorted(providers.get(name, ())), clients=sorted(clients.get(name, ())))
                for name, types in sorted(services.items())]

    return gateway.cached('services', produce, lifetime=1.0)


def interface(type_name):
    """Describe a service type: the request and response schemas and the request's default values."""
    service_type = service_class(type_name)
    return dict(type=type_name, request=ros_interfaces.describe(service_type.Request),
                request_default=ros_interfaces.defaults(service_type.Request),
                response=ros_interfaces.describe(service_type.Response))


class ServiceCaller:
    """Make confirmed service calls, one client per call, and keep the call records of this session."""

    def __init__(self, gateway):
        """Start with no records for a running gateway."""
        self.gateway = gateway
        self.lock = threading.Lock()
        self.records = {}
        self.order = []
        self.requests = {}

    def list(self):
        """Return the call records of this session, newest first."""
        with self.lock:
            return [dict(self.records[call_id]) for call_id in reversed(self.order)]

    def get(self, call_id):
        """Return one call record, or ``None`` for an unknown identifier."""
        with self.lock:
            record = self.records.get(call_id)
            return dict(record) if record else None

    def update(self, record, **fields):
        """Change a record under the lock, so readers never see half an update."""
        with self.lock:
            record.update(fields)

    def call(self, service, type_name, request_value, request_id, timeout):
        """Call one service once with checked values and return its record; a repeated request id calls nothing.

        First look up the request id: a known one returns the earlier record
        marked as a duplicate. Then resolve the service type and build the
        request; invalid values raise ``InvalidValue`` and nothing is called.
        Create a client on the ROS thread and wait up to two seconds for a
        provider; without one the call is refused and nothing is sent. Then
        send the request once and record the call as pending. Wait on this
        (web worker) thread, never on the ROS thread, until the response
        arrives, the timeout passes, or the provider disappears. A response
        makes the call ``succeeded``; no response in time makes it
        ``timed_out``; a provider that vanished makes it ``server_lost``. In
        the last two cases the outcome is unknown and the call is not
        repeated. The client is always destroyed afterwards, so an idle GUI
        holds no service clients.
        """
        with self.lock:
            if request_id in self.requests:
                return dict(self.records[self.requests[request_id]], duplicate=True)
        service_type = service_class(type_name)
        message = ros_interfaces.build(service_type.Request, request_value)
        holder = {}

        def create():
            """Create the client for this one call on the ROS thread."""
            holder['client'] = self.gateway.node.create_client(service_type, service)
            return holder['client']

        client = self.gateway.call(create)
        try:
            deadline = time.monotonic() + READY_WAIT
            while not self.gateway.call(client.service_is_ready):
                if time.monotonic() > deadline:
                    raise ServiceCallRefused(f'No provider is available for {service} ({type_name}); nothing was sent', 409)
                time.sleep(0.1)
            return self.send(client, service, type_name, request_value, message, request_id, timeout)
        finally:
            try:
                self.gateway.call(lambda: self.gateway.node.destroy_client(client))
            except GatewayError:
                pass

    def send(self, client, service, type_name, request_value, message, request_id, timeout):
        """Send the request once, wait a bounded time, and record what happened.

        The record is created and registered under its request id before the
        request leaves, so a repeated request that arrives meanwhile finds it
        instead of calling again. While waiting, the provider's presence is
        checked twice a second; if it is gone for a second, waiting stops.
        """
        answered = threading.Event()
        record = dict(call_id=uuid.uuid4().hex, request_id=request_id, service=service, type=type_name,
                      request=request_value, state='pending', response=None, error=None, timeout=timeout,
                      created_at=time.time(), finished_at=None)
        with self.lock:
            self.records[record['call_id']] = record
            self.order.append(record['call_id'])
            self.requests[request_id] = record['call_id']
            for old in self.order[:-CALLS_KEPT]:
                self.records.pop(old, None)
            del self.order[:-CALLS_KEPT]

        def start():
            """Send the request on the ROS thread and arrange to be told about the answer."""
            future = client.call_async(message)
            future.add_done_callback(lambda done: answered.set())
            return future

        future = self.gateway.call(start)
        deadline = time.monotonic() + timeout
        missing_since = None
        while not answered.wait(0.25):
            now = time.monotonic()
            if now > deadline:
                self.update(record, state='timed_out', finished_at=time.time(),
                            error=f'No response within {timeout:g} s. The provider may still have acted; '
                                  'the call is not repeated.')
                return dict(record)
            try:
                ready = self.gateway.call(client.service_is_ready, timeout=1.0)
            except GatewayError:
                ready = True
            missing_since = None if ready else (missing_since or now)
            if missing_since is not None and now - missing_since > 1.0:
                self.update(record, state='server_lost', finished_at=time.time(),
                            error='The provider disappeared before answering. It may have acted; '
                                  'the call is not repeated.')
                return dict(record)
        try:
            response = ros_interfaces.to_json(future.result())
            self.update(record, state='succeeded', response=response, finished_at=time.time())
        except Exception as error:  # the provider raised or the call failed inside rclpy
            self.update(record, state='failed', error=f'{type(error).__name__}: {error}', finished_at=time.time())
        return dict(record)
