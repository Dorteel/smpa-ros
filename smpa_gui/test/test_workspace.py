"""Backend tests of the data behind the ROS 2 Workspace, against real ROS 2 fixture nodes.

The Workspace reuses the existing discovery routes for topics, services and
actions, and adds one route, ``GET /api/v1/ros/parameters``, that reads the
parameters of every discovered node. These tests check that each route
reports the fixture interfaces with their types and owners, that
interfaces appear and disappear with the node that offers them, and that
an unresponsive node delays the parameter overview by one timeout only.
"""
import subprocess
import time

import pytest

from conftest import GUI, PYTHON, fetch, stop
from test_management import SETTINGS, start_fixture, wait_until

DYNAMIC_NODE = '/fixture_workspace/dynamic'


def start_dynamic(environment):
    """Start the dynamic Workspace fixture node and wait until it says it exists."""
    process = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/workspace_nodes.py')], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = process.stdout.readline()
    if 'WORKSPACE READY' not in line:
        process.kill()
        raise RuntimeError('Workspace fixture failed: ' + line + process.stdout.read())
    return process


def interfaces(base):
    """Return the names the Workspace would show: topics, services, actions and (node, parameter) pairs."""
    overview = fetch(base + '/api/v1/ros/parameters', timeout=15)
    return dict(topics={topic['name'] for topic in fetch(base + '/api/v1/ros/topics')['topics']},
                services={service['name'] for service in fetch(base + '/api/v1/ros/services')['services']},
                actions={action['name'] for action in fetch(base + '/api/v1/ros/actions')['actions']},
                parameters={(reading['node'], parameter['name']) for reading in overview['nodes']
                            for parameter in reading['parameters']},
                nodes={reading['node'] for reading in overview['nodes']})


@pytest.fixture(scope='module')
def world(ros_environment, server):
    """Run the management fixtures beside the shared GUI server, so settings and services exist too."""
    process = start_fixture(ros_environment)
    wait_until(lambda: fetch(f'{server}/api/v1/ros/nodes/parameters?node={SETTINGS}')['state'] == 'ok',
               message='the settings node never answered parameter requests')
    yield dict(base=server, environment=ros_environment)
    stop(process)


def test_topics_services_and_actions_report_names_and_types(world):
    """The routes the top, left and right panels use list the fixture interfaces with their types and providers."""
    topics = {topic['name']: topic for topic in fetch(world['base'] + '/api/v1/ros/topics')['topics']}
    assert topics['/fixture/chatter']['types'] == ['std_msgs/msg/String']
    assert topics['/fixture/chatter']['publisher_count'] >= 1
    assert topics['/fixture/scan']['types'] == ['sensor_msgs/msg/LaserScan']
    services = {service['name']: service for service in fetch(world['base'] + '/api/v1/ros/services')['services']}
    assert services['/fixture/add_two_ints']['types'] == ['example_interfaces/srv/AddTwoInts']
    assert services['/fixture/add_two_ints']['kind'] == 'other' and services['/fixture/add_two_ints']['providers']
    actions = {action['name']: action for action in fetch(world['base'] + '/api/v1/ros/actions')['actions']}
    assert actions['/fixture/fibonacci']['types'] == ['example_interfaces/action/Fibonacci']
    assert actions['/fixture/fibonacci']['servers'] == ['/fixture_action_server']


def test_parameter_overview_keeps_parameters_with_their_nodes(world):
    """Every visible node is listed with its own parameters, values and types; states explain the rest."""
    started = time.monotonic()
    overview = fetch(world['base'] + '/api/v1/ros/parameters', timeout=15)
    elapsed = time.monotonic() - started
    readings = {reading['node']: reading for reading in overview['nodes']}
    owned = {parameter['name']: parameter for parameter in readings['/fixture_ns/fixture_parameters']['parameters']}
    assert owned['fixture_integer']['value'] == 42 and owned['fixture_integer']['type'] == 'integer'
    assert owned['fixture_string']['value'] == 'hello' and owned['fixture_double']['value'] == 1.5
    assert owned['fixture_flags']['value'] == [True, False] and owned['fixture_flags']['type'] == 'bool_array'
    settings = {parameter['name']: parameter for parameter in readings[SETTINGS]['parameters']}
    assert settings['frozen']['read_only'] is True and 'fixture_integer' not in settings
    assert readings['/fixture_silent'] == dict(node='/fixture_silent', state='not_responding', parameters=[], is_gateway=False)
    assert readings['/smpa_gui']['is_gateway'] is True and readings['/smpa_gui']['state'] == 'unavailable'
    assert all(not node.split('/')[-1].startswith('_') for node in readings)
    # The silent node costs one timeout (1 s by default), not one per node.
    assert overview['timeout'] == 1.0 and elapsed < 6.0


def test_parameter_overview_timeout_is_bounded(world):
    """The timeout can be chosen within limits; values outside them are refused."""
    assert fetch(world['base'] + '/api/v1/ros/parameters?timeout=0.3', timeout=15)['timeout'] == 0.3
    from urllib.error import HTTPError
    with pytest.raises(HTTPError) as refused:
        fetch(world['base'] + '/api/v1/ros/parameters?timeout=30')
    assert refused.value.code == 422


def test_interfaces_appear_and_disappear_with_their_node(world):
    """Starting a node adds its topic, service, action and parameter to every route; stopping it removes them."""
    names = dict(topics='/fixture_ws/dynamic_topic', services='/fixture_ws/dynamic_service',
                 actions='/fixture_ws/dynamic_action', parameters=(DYNAMIC_NODE, 'dynamic_threshold'))
    process = start_dynamic(world['environment'])
    try:
        wait_until(lambda: all(names[kind] in interfaces(world['base'])[kind] for kind in names),
                   message='the dynamic interfaces never appeared')
        reading = next(item for item in fetch(world['base'] + '/api/v1/ros/parameters', timeout=15)['nodes']
                       if item['node'] == DYNAMIC_NODE)
        assert reading['state'] == 'ok'
        assert {parameter['name']: parameter['value'] for parameter in reading['parameters']}['dynamic_threshold'] == 0.5
    finally:
        stop(process)
    wait_until(lambda: not any(names[kind] in interfaces(world['base'])[kind] for kind in names)
               and DYNAMIC_NODE not in interfaces(world['base'])['nodes'],
               message='the dynamic interfaces never disappeared')


def test_read_many_reads_side_by_side_and_isolates_failures(monkeypatch):
    """Nodes are read in parallel, keep their order, and one failing node does not hide the others."""
    from smpa_gui.ros_adapter import ros_parameters

    def fake_read(gateway, node_name, timeout):
        """Pretend to read a node: slow ones take the whole timeout, '/broken' fails."""
        if node_name == '/broken':
            raise RuntimeError('gateway stopped')
        time.sleep(timeout)
        return dict(node=node_name, state='not_responding', parameters=[])

    monkeypatch.setattr(ros_parameters, 'read', fake_read)
    names = [f'/slow_{index}' for index in range(6)] + ['/broken']
    started = time.monotonic()
    readings = ros_parameters.read_many(None, names, timeout=0.5)
    assert time.monotonic() - started < 1.5
    assert [reading['node'] for reading in readings] == names
    assert readings[-1] == dict(node='/broken', state='error', detail='gateway stopped', parameters=[])
    assert ros_parameters.read_many(None, [], timeout=0.5) == []
