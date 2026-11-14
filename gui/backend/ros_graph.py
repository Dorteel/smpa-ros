"""Discover the ROS 2 computation graph and describe it for the browser.

Concepts:
    Raw discovery is what the ROS graph API reports: node names, topic names
    with types, and one endpoint record per publisher or subscription.
    The graph description is the documented JSON shape built from it: nodes,
    topics, publish/subscribe edges, actions, and services. Building it is a
    pure function so it can be tested without ROS.
"""
import time

from backend import CONTRACT_VERSION
from backend.ros_names import INFRASTRUCTURE_TOPICS, action_of, full_node_name, is_hidden


def duration_seconds(duration):
    """Convert a QoS duration to seconds, or ``None`` when it means 'unlimited'.

    ROS reports an unset deadline or lifespan as a very large duration.
    Anything above one hundred years is treated as unlimited.
    """
    seconds = duration.nanoseconds / 1e9
    return None if seconds > 3.15e9 else seconds


def qos_description(profile):
    """Describe a quality-of-service profile with plain names and numbers.

    Report the policies that decide whether two endpoints can communicate:
    reliability, durability, history with depth, and the liveliness, deadline
    and lifespan settings.
    """
    return dict(reliability=profile.reliability.name.lower(),
                durability=profile.durability.name.lower(),
                history=profile.history.name.lower(), depth=profile.depth,
                liveliness=profile.liveliness.name.lower(),
                deadline_s=duration_seconds(profile.deadline),
                lifespan_s=duration_seconds(profile.lifespan))


def endpoint_description(info):
    """Describe one publisher or subscription endpoint reported by discovery."""
    return dict(node=full_node_name(info.node_namespace, info.node_name),
                type=info.topic_type, qos=qos_description(info.qos_profile))


def collect(node):
    """Ask ROS for everything currently discoverable; must run on the ROS thread.

    Read node names, topic names and types, the publisher and subscription
    endpoints of every topic, service names and types, and action names and
    types. Return plain data only, so the result can leave the ROS thread.
    Entities that vanish between two questions simply appear in one list and
    not the other; the builder tolerates that.
    """
    from rclpy.action import get_action_names_and_types

    topics = []
    for name, types in node.get_topic_names_and_types():
        topics.append(dict(
            name=name, types=list(types),
            publishers=[endpoint_description(info) for info in node.get_publishers_info_by_topic(name)],
            subscribers=[endpoint_description(info) for info in node.get_subscriptions_info_by_topic(name)]))
    return dict(
        nodes=[dict(name=name, namespace=namespace)
               for name, namespace in node.get_node_names_and_namespaces()],
        topics=topics,
        services=[dict(name=name, types=list(types)) for name, types in node.get_service_names_and_types()],
        actions=[dict(name=name, types=list(types)) for name, types in get_action_names_and_types(node)])


def build(raw, gateway_name=None):
    """Turn raw discovery into the documented graph description.

    Every node becomes one entry; nodes named only by an endpoint are added
    too. Every topic becomes one entry marked as hidden, infrastructure, or
    part of an action where that applies. Each publisher becomes an edge from
    node to topic and each subscription an edge from topic to node. Action
    servers are the publishers of an action's status topic and action clients
    are its subscribers. The gateway's own node is flagged so that views can
    leave the observer out of the picture. Output is sorted to be stable
    between refreshes.
    """
    nodes = {}

    def node_entry(full_name):
        """Return the entry for a node name, creating it on first mention."""
        if full_name not in nodes:
            namespace, _, name = full_name.rpartition('/')
            nodes[full_name] = dict(id='node:' + full_name, name=name, namespace=namespace or '/',
                                    full_name=full_name, hidden=is_hidden(full_name),
                                    is_gateway=full_name == gateway_name)
        return nodes[full_name]

    for item in raw['nodes']:
        node_entry(full_node_name(item['namespace'], item['name']))

    topics, edges = [], []
    action_servers, action_clients = {}, {}
    for topic in sorted(raw['topics'], key=lambda item: item['name']):
        name = topic['name']
        action = action_of(name)
        topics.append(dict(id='topic:' + name, name=name, types=topic['types'], hidden=is_hidden(name),
                           infrastructure=name in INFRASTRUCTURE_TOPICS, action=action,
                           publisher_count=len(topic['publishers']),
                           subscriber_count=len(topic['subscribers'])))
        for kind, endpoints in (('publishes', topic['publishers']), ('subscribes', topic['subscribers'])):
            for index, endpoint in enumerate(endpoints):
                owner = node_entry(endpoint['node'])
                source, target = ((owner['id'], 'topic:' + name) if kind == 'publishes'
                                  else ('topic:' + name, owner['id']))
                edges.append(dict(id=f"{kind}:{endpoint['node']}:{name}:{index}", source=source,
                                  target=target, kind=kind, type=endpoint['type'], qos=endpoint['qos']))
                if action and name.endswith('/_action/status'):
                    group = action_servers if kind == 'publishes' else action_clients
                    group.setdefault(action, set()).add(endpoint['node'])

    actions = [dict(id='action:' + item['name'], name=item['name'], types=item['types'],
                    servers=sorted(action_servers.get(item['name'], ())),
                    clients=sorted(action_clients.get(item['name'], ())))
               for item in sorted(raw['actions'], key=lambda item: item['name'])]
    services = [dict(name=item['name'], types=item['types'], hidden=is_hidden(item['name']),
                     action=action_of(item['name']))
                for item in sorted(raw['services'], key=lambda item: item['name'])]
    return dict(contract_version=CONTRACT_VERSION, stamp=time.time(),
                nodes=sorted(nodes.values(), key=lambda item: item['full_name']),
                topics=topics, edges=sorted(edges, key=lambda item: item['id']),
                actions=actions, services=services)


def snapshot(gateway):
    """Return the current graph description, reusing a result up to one second old."""
    def produce():
        """Collect on the ROS thread, then build outside it."""
        return build(gateway.call(lambda: collect(gateway.node)), gateway.full_name())

    return gateway.cached('graph', produce, lifetime=1.0)
