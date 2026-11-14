#!/usr/bin/env python3
"""Save observed ROS discovery and a genuine rqt_graph capture for Sense.

The SVG uses only discovered topic publishers/subscribers. Service and action
endpoints are inventoried, but discovery is not evidence of an RPC invocation.
Python calls, file access and hypothetical connections are never drawn.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


def discover(prefix, seconds):
    """Snapshot actual nodes, interfaces and topic endpoint QoS through ROS.

    Allow discovery to settle for the requested interval. Select run-owned Sense
    nodes, retain their service/client/action inventory, and query topic endpoints
    directly. Always destroy the temporary observer and its ROS context; no
    sensor, inference or robot action is invoked.
    """
    import rclpy
    from rclpy.action.graph import (get_action_server_names_and_types_by_node,
                                   get_action_client_names_and_types_by_node)
    rclpy.init()
    node = rclpy.create_node('sense_graph_inventory')
    result = dict(captured_at=datetime.now(timezone.utc).isoformat(), nodes=[], topics=[])
    try:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            rclpy.spin_once(node, timeout_sec=min(.2, max(0, until-time.monotonic())))
        for name, namespace in sorted(set(node.get_node_names_and_namespaces())):
            full = namespace.rstrip('/') + '/' + name
            if not full.startswith(prefix) or '/sense/' not in full:
                continue
            row = dict(name=full)
            for key, operation in (
                ('publishers', node.get_publisher_names_and_types_by_node),
                ('subscriptions', node.get_subscriber_names_and_types_by_node),
                ('services', node.get_service_names_and_types_by_node),
                ('clients', node.get_client_names_and_types_by_node),
                ('action_servers', lambda n, ns: get_action_server_names_and_types_by_node(node, n, ns)),
                ('action_clients', lambda n, ns: get_action_client_names_and_types_by_node(node, n, ns)),
            ):
                row[key] = operation(name, namespace)
            result['nodes'].append(row)
        selected = {row['name'] for row in result['nodes']}
        for topic, types in node.get_topic_names_and_types():
            if not (topic.startswith('/tiago/camera/') or (topic.startswith(prefix) and '/sense/' in topic)):
                continue
            row = dict(name=topic, types=types)
            for key, operation in (('publishers', node.get_publishers_info_by_topic),
                                   ('subscriptions', node.get_subscriptions_info_by_topic)):
                row[key] = []
                for endpoint in operation(topic):
                    full = endpoint.node_namespace.rstrip('/') + '/' + endpoint.node_name
                    qos = endpoint.qos_profile
                    row[key].append(dict(node=full, reliability=qos.reliability.name,
                                         durability=qos.durability.name, history=qos.history.name,
                                         depth=qos.depth))
            endpoints = row['publishers'] + row['subscriptions']
            if any(endpoint['node'] in selected for endpoint in endpoints):
                result['topics'].append(row)
        return result
    finally:
        node.destroy_node()
        rclpy.shutdown()


def topic_dot(inventory):
    """Render a filtered topic graph exclusively from observed endpoint ownership.

    Include every discovered Sense node, even isolated file-backed or service
    nodes. Add external nodes only when their observed publication/subscription
    connects to a selected topic. Draw each discovered publisher/topic/subscriber
    relation separately; no edge asserts payload delivery or service execution.
    """
    quoted = json.dumps
    lines = ['digraph Sense {',
             'graph [rankdir=LR, bgcolor="white", pad=.3, nodesep=.4, ranksep=.6,',
             'fontname="DejaVu Sans", fontsize=16, labelloc=t,',
             'label="Observed Sense ROS topic endpoints\\nEdges show discovery, not proof of message delivery. No Python/file/RPC edges."];',
             'node [fontname="DejaVu Sans", fontsize=14, margin=".15,.1"];',
             'edge [color="#365e80", penwidth=1.5];']
    selected = {row['name'] for row in inventory['nodes']}
    nodes = set(selected)
    for topic in inventory['topics']:
        for endpoint in topic['publishers'] + topic['subscriptions']:
            nodes.add(endpoint['node'])
    for node in sorted(nodes):
        color = '#e7f1fb' if node in selected else '#f1f1f1'
        lines.append(f'{quoted("node:"+node)} [shape=ellipse, style=filled, fillcolor="{color}", label={quoted(node)}];')
    for topic in inventory['topics']:
        identity = quoted('topic:' + topic['name'])
        label = topic['name'] + '\n' + ', '.join(topic['types'])
        lines.append(f'{identity} [shape=box, label={quoted(label)}];')
        for endpoint in topic['publishers']:
            lines.append(f'{quoted("node:"+endpoint["node"])} -> {identity};')
        for endpoint in topic['subscriptions']:
            lines.append(f'{identity} -> {quoted("node:"+endpoint["node"])};')
    return '\n'.join(lines + ['}'])


def screenshot(output, prefix):
    """Open the installed rqt_graph plugin and capture its real discovered view.

    Configure the plugin's existing UI filters after initialization, show all
    topic endpoints, and retain isolated Sense nodes. Save the actual Qt widget
    and plugin DOT after discovery refresh. This is a separate runtime snapshot
    from the machine inventory and may miss transient subscriptions. Quit only
    this inspection application; fail explicitly if no screenshot was saved.
    """
    from python_qt_binding.QtCore import QTimer
    from python_qt_binding.QtWidgets import QApplication
    from rqt_graph.ros_graph import RosGraph
    from rqt_gui.main import Main
    original = RosGraph.__init__

    def initialize(plugin, context):
        """Initialize the real plugin, then schedule its filtered runtime capture."""
        original(plugin, context)

        def save():
            """Refresh live discovery and save the actual widget without synthetic edges."""
            plugin.initialized = True
            plugin._widget.graph_type_combo_box.setCurrentIndex(2)
            plugin._widget.filter_line_edit.setText(prefix + '.*/sense/.*, /TIAGo')
            plugin._widget.topic_filter_line_edit.setText(prefix + '.*/sense/.*, /tiago/camera/.*')
            for name in ('dead_sinks_check_box', 'leaf_topics_check_box', 'quiet_check_box', 'unreachable_check_box'):
                getattr(plugin._widget, name).setChecked(False)
            plugin._update_rosgraph()
            plugin._widget.resize(1900, 1100)
            plugin._fit_in_view()
            plugin._widget.grab().save(str(output / 'rqt_graph.png'))
            (output / 'rqt_graph.dot').write_text(plugin._current_dotcode)
            QApplication.instance().quit()

        QTimer.singleShot(8000, save)

    RosGraph.__init__ = initialize
    try:
        Main().main([sys.argv[0]], standalone='rqt_graph.ros_graph.RosGraph')
    finally:
        RosGraph.__init__ = original
    if not (output / 'rqt_graph.png').is_file():
        raise RuntimeError('rqt_graph did not save its screenshot')


def main():
    """Capture one Sense inventory, observed-only SVG and optional real screenshot.

    Accept a selected run prefix or all run prefixes and an explicit artifact
    directory. Discovery and graph export are read-only toward the robot. An
    empty graph is a failure, not a fabricated architecture. Graphviz and rqt
    are verification tools already supplied by the ROS environment; nothing is
    installed by this command. Use QT_QPA_PLATFORM=offscreen for unattended Qt.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-prefix', default='/run_')
    parser.add_argument('--discovery-seconds', type=float, default=4)
    parser.add_argument('--no-screenshot', action='store_true')
    options = parser.parse_args()
    if not options.run_prefix.startswith('/run_') or options.discovery_seconds <= 0:
        parser.error('Use a /run_ prefix and positive discovery interval')
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    inventory = discover(options.run_prefix, options.discovery_seconds)
    (output / 'interfaces.json').write_text(json.dumps(inventory, indent=2))
    if not inventory['nodes']:
        raise SystemExit('No run-owned Sense nodes discovered; start SMPA before capturing')
    dot = output / 'sense-runtime.dot'
    dot.write_text(topic_dot(inventory))
    subprocess.run(['dot', '-Tsvg', str(dot), '-o', str(output / 'sense-runtime.svg')], check=True)
    if not options.no_screenshot:
        screenshot(output, options.run_prefix)
    print(f'Observed artifacts saved to {output}')


if __name__ == '__main__':
    main()
