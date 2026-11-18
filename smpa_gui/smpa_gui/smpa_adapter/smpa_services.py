"""Read the state of SMPA's existing ROS 2 interfaces: run control and the shared inference server.

Concepts:
    SMPA's ``smpa_control`` node (package ``smpa_ros``) reports the robot's
    run through the ``GetSmpaStatus`` service and announces stage and run
    events as ``SmpaEvent`` messages. SMPA's ``llm`` node (package
    ``smpa_llm``) lists registered models (``ListModels``), reports its
    state and active model (``GetModelStatus``) and publishes
    ``InferenceStatus``. All types come from the installed
    ``smpa_ros_interfaces`` package; nothing is redefined.

    Endpoints are the ROS names of those services and topics. The defaults
    are the names the nodes use when launched without a namespace; each can
    be changed in the layout file's ``ros.endpoints`` section, for example
    when SMPA runs in a namespace.

    The GUI only reads here. It does not start or stop runs, load models or
    change model settings; those stay with SMPA and its ROS parameters.
"""
import asyncio

from smpa_gui.ros_adapter.ros_clients import RosCallFailed

DEFAULT_ENDPOINTS = dict(
    smpa_get_status='/smpa/get_status',
    smpa_events='/smpa/events',
    llm_list_models='/llm/list_models',
    llm_get_model_status='/llm/get_model_status',
    llm_inference_status='/llm/inference_status',
)
TYPES = dict(
    smpa_get_status='smpa_ros_interfaces/srv/GetSmpaStatus',
    smpa_events='smpa_ros_interfaces/msg/SmpaEvent',
    llm_list_models='smpa_ros_interfaces/srv/ListModels',
    llm_get_model_status='smpa_ros_interfaces/srv/GetModelStatus',
    llm_inference_status='smpa_ros_interfaces/msg/InferenceStatus',
)
RELAYS = (('smpa_events', 'smpa_event'), ('llm_inference_status', 'inference_status'))


def endpoints(section):
    """Return the endpoint names from the layout file's ``ros`` section, completed with the defaults.

    The section may contain only ``endpoints``, a mapping from the names in
    ``DEFAULT_ENDPOINTS`` to absolute ROS names. Anything else raises
    ``ValueError`` naming the problem.
    """
    section = section or {}
    if not isinstance(section, dict) or set(section) - {'endpoints'}:
        raise ValueError('ros accepts only: endpoints')
    chosen = section.get('endpoints') or {}
    if not isinstance(chosen, dict) or set(chosen) - set(DEFAULT_ENDPOINTS):
        raise ValueError(f'ros.endpoints accepts only: {", ".join(DEFAULT_ENDPOINTS)}')
    for key, value in chosen.items():
        if not isinstance(value, str) or not value.startswith('/'):
            raise ValueError(f'ros.endpoints.{key} must be an absolute ROS name')
    return dict(DEFAULT_ENDPOINTS, **chosen)


def failure(error, endpoint):
    """Describe a failed status request with the endpoint, the reason and the explanation."""
    return dict(state=error.state, endpoint=endpoint, detail=str(error))


async def robot_status(clients, names, timeout=2.0):
    """Ask SMPA's run control for the robot's run: its state, run id and active stage.

    Returns ``state`` "ok" with ``run_state``, ``run_id`` and
    ``active_stage``, or the reason the question could not be answered
    (for example ``unavailable`` when no run-control node is running).
    """
    try:
        answer = await clients.call_service(names['smpa_get_status'], TYPES['smpa_get_status'], timeout=timeout)
    except RosCallFailed as error:
        return failure(error, names['smpa_get_status'])
    return dict(state='ok', endpoint=names['smpa_get_status'], run_state=answer['state'],
                run_id=answer['run_id'] or None, active_stage=answer['active_stage'] or None)


async def inference_status(clients, names, timeout=2.0):
    """Ask SMPA's inference server for its registered models and its current state, side by side.

    Each question is answered on its own, so a missing service shows as a
    reason without hiding the other answer.
    """
    async def models():
        """List the registered model aliases."""
        try:
            answer = await clients.call_service(names['llm_list_models'], TYPES['llm_list_models'], timeout=timeout)
        except RosCallFailed as error:
            return failure(error, names['llm_list_models'])
        return dict(state='ok', endpoint=names['llm_list_models'], model_ids=answer['model_ids'])

    async def current():
        """Read the server's state and active model."""
        try:
            answer = await clients.call_service(names['llm_get_model_status'], TYPES['llm_get_model_status'],
                                                timeout=timeout)
        except RosCallFailed as error:
            return failure(error, names['llm_get_model_status'])
        return dict(state='ok', endpoint=names['llm_get_model_status'], server_state=answer['state'],
                    active_model=answer['active_model'] or None)

    listed, status = await asyncio.gather(models(), current())
    return dict(models=listed, status=status)


async def robot_run(clients, names):
    """Return the run id of the robot's current run, or None when none is running or run control is absent."""
    status = await robot_status(clients, names)
    if status['state'] == 'ok' and status['run_state'] in ('running', 'stopping') and status['run_id']:
        return status['run_id']
    return None
