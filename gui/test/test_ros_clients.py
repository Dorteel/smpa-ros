"""Tests of the asynchronous ROS 2 client layer (``ros_adapter.ros_clients``) inside one process.

A real gateway node joins the test's ROS domain beside the stand-in nodes
of ``fixtures/cognitive_nodes.py``, which use SMPA's real interface package
and run no inference. Skipped when ``smpa_ros_interfaces`` is not installed.
"""
import asyncio
import time

import pytest

from conftest import stop
from test_dialogue_ros import ROBOT_RUN, interfaces_installed, start_stand_ins, write_memory

pytestmark = pytest.mark.skipif(not interfaces_installed(),
                                reason='smpa_ros_interfaces is not installed (source the SMPA workspace)')


@pytest.fixture(scope='module')
def monkeypatch_module():
    """Provide a monkeypatch that lasts for the whole module."""
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture(scope='module')
def clients(ros_environment, tmp_path_factory, monkeypatch_module):
    """Start the stand-ins and a gateway in this process (in the test's ROS domain); yield its RosClients."""
    for key in ('ROS_DOMAIN_ID', 'ROS_AUTOMATIC_DISCOVERY_RANGE'):
        monkeypatch_module.setenv(key, ros_environment[key])
    memory = write_memory(tmp_path_factory.mktemp('clients') / 'memory')
    stand_ins = start_stand_ins(ros_environment, memory)
    from smpa_gui.ros_adapter.gateway import Gateway
    from smpa_gui.ros_adapter.ros_clients import RosClients
    gateway = Gateway('ros_clients_test')
    gateway.start()
    yield RosClients(gateway)
    gateway.stop()
    stop(stand_ins)


def outcome(coroutine):
    """Run a coroutine and return ('ok', value) or (failure state, message, details)."""
    from smpa_gui.ros_adapter.ros_clients import RosCallFailed

    async def run():
        """Await the call and turn a RosCallFailed into a tuple."""
        try:
            return 'ok', await coroutine
        except RosCallFailed as error:
            return error.state, str(error), error.details
    return asyncio.run(run())


def test_service_calls_use_the_installed_types(clients):
    """ListModels and GetSmpaStatus are called with SMPA's real types; the answers come back as JSON data."""
    state, models = outcome(clients.call_service('/llm/list_models', 'smpa_ros_interfaces/srv/ListModels', patience=5))
    assert (state, models) == ('ok', dict(model_ids=['stub-a', 'stub-b']))
    state, status = outcome(clients.call_service('/smpa/get_status', 'smpa_ros_interfaces/srv/GetSmpaStatus'))
    assert state == 'ok' and status == dict(state='running', run_id=ROBOT_RUN, active_stage='model')
    assert clients.counts()['clients'] == 0  # every client is destroyed after its call


def test_action_goal_returns_result_and_acceptance(clients):
    """A LoadModel goal is accepted and returns its result; acceptance reaches the event loop."""
    events = []
    state, answer = outcome(clients.run_action('/llm/load_model', 'smpa_ros_interfaces/action/LoadModel',
                                               dict(model_id='stub-b'), timeout=10, patience=5,
                                               on_event=lambda kind, details: events.append(kind)))
    assert state == 'ok' and answer['state'] == 'succeeded' and answer['result'] == dict(success=True, error_message='')
    assert events[:1] == ['accepted']
    state, answer = outcome(clients.run_action('/llm/load_model', 'smpa_ros_interfaces/action/LoadModel',
                                               dict(model_id='unknown'), timeout=10))
    assert answer['result'] == dict(success=False, error_message='Unknown registered model: unknown')
    assert clients.counts()['clients'] == 0


def test_failures_are_classified(clients):
    """Missing types, bad requests, absent servers, aborted goals and timeouts each give their own state."""
    assert outcome(clients.call_service('/x', 'smpa_ros_interfaces/srv/NoSuchService'))[0] == 'interface_missing'
    state, _, details = outcome(clients.run_action('/smpa/sense', 'smpa_ros_interfaces/action/Sense', dict(run_id=5)))
    assert state == 'invalid_request' and details['errors'][0]['path'] == 'run_id'
    started = time.monotonic()
    assert outcome(clients.call_service('/nobody/here', 'smpa_ros_interfaces/srv/ListModels', patience=0.5))[0] == 'unavailable'
    assert time.monotonic() - started < 3
    assert outcome(clients.run_action('/smpa/sense', 'smpa_ros_interfaces/action/Sense',
                                      dict(run_id='00000000-0000-0000-0000-00000000dead'), timeout=10))[0] == 'aborted'
    state, message, _ = outcome(clients.run_action('/llm/load_model', 'smpa_ros_interfaces/action/LoadModel',
                                                   dict(model_id='stub-a'), timeout=0.2))
    assert state == 'timed_out' and 'outcome is unknown' in message
    time.sleep(1.0)  # let the stand-in finish the load it kept running
    assert clients.counts()['clients'] == 0


def test_actions_run_concurrently_without_blocking_the_event_loop(clients):
    """Two half-second goals finish together in well under a second, and the event loop keeps ticking meanwhile."""
    async def scenario():
        """Start two loads, count loop ticks while they run, and time them."""
        started = time.monotonic()
        both = asyncio.gather(*(clients.run_action('/llm/load_model', 'smpa_ros_interfaces/action/LoadModel',
                                                   dict(model_id='stub-a'), timeout=10) for _ in range(2)))
        ticks = 0
        while not both.done():
            await asyncio.sleep(0.01)
            ticks += 1
        results = await both
        return ticks, time.monotonic() - started, results
    ticks, elapsed, results = asyncio.run(scenario())
    assert ticks > 20 and elapsed < 0.95 + 0.6 and all(result['state'] == 'succeeded' for result in results)


def test_relay_hands_messages_over_and_releases(clients):
    """A relay delivers SmpaEvent messages as JSON data and its subscription ends with the last listener."""
    received = []
    release = clients.relay('/smpa/events', 'smpa_ros_interfaces/msg/SmpaEvent', received.append)
    deadline = time.monotonic() + 10
    while not received and time.monotonic() < deadline:
        time.sleep(0.1)
    release()
    assert received[0] == dict(run_id=ROBOT_RUN, stage='model', event='stage_started')
    assert clients.counts()['relays'] == 0
    from smpa_gui.ros_adapter.ros_clients import RosCallFailed
    with pytest.raises(RosCallFailed) as missing:
        clients.relay('/x', 'smpa_ros_interfaces/msg/NoSuchMessage', received.append)
    assert missing.value.state == 'interface_missing'
