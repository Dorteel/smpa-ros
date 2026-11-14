"""Standalone, observational ROS 2 gateway for the browser GUI.

Concepts:
    The gateway is one ROS 2 node in its own process. It discovers the ROS
    graph and subscribes to topics only while a browser view needs them. It
    never publishes application data, calls application services, sends
    action goals, or sets parameters. It imports nothing from SMPA.

    The contract version names the JSON shapes documented in ``gui/API.md``.
    Every HTTP response and every stream message follows that document.
"""

CONTRACT_VERSION = '1.0'
