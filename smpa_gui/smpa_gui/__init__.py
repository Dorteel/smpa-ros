"""The ``smpa_gui`` ROS 2 package: a browser GUI for ROS 2 systems, optionally showing SMPA.

Concepts:
    The gateway is one ROS 2 node in its own process. It discovers the ROS
    graph and subscribes to topics only while a browser view needs them. It
    acts on ROS only when a local user explicitly confirms it: sending and
    cancelling action goals, setting parameters through the standard
    parameter services, and calling services. It never publishes application
    data. Everything ROS-specific lives in ``ros_adapter``.

    SMPA is optional. ``smpa_adapter`` reads SMPA's recorded run directories
    and Turtle graphs read-only, and runs SMPA's own conversation server as a
    separate process to relay the Dashboard's conversation to it. Nothing of
    SMPA is imported, and the ROS functionality works without it.

    The contract version names the JSON shapes documented in ``API.md``.
    Every HTTP response and every stream message follows that document.
    Version 1.5 merged the conversation and graph routes of GUI version 4
    (1.3) into version 5 (1.4); version 1.6 adds the parameter overview
    ``GET /api/v1/ros/parameters`` used by the ROS 2 Workspace.
"""

CONTRACT_VERSION = '1.6'
