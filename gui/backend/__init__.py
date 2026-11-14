"""Standalone ROS 2 gateway for the browser GUI: observation plus explicit action goals.

Concepts:
    The gateway is one ROS 2 node in its own process. It discovers the ROS
    graph and subscribes to topics only while a browser view needs them. Its
    only way of acting on ROS is sending and cancelling action goals, and only
    when a local user explicitly asks for it. It never publishes application
    data, calls application services, or sets parameters. It imports nothing
    from SMPA.

    The contract version names the JSON shapes documented in ``gui/API.md``.
    Every HTTP response and every stream message follows that document.
"""

CONTRACT_VERSION = '1.1'
