"""The ``smpa_gui`` ROS 2 package: a browser GUI for ROS 2 systems, optionally showing SMPA.

Concepts:
    The gateway is one ROS 2 node in its own process. It discovers the ROS
    graph and subscribes to topics only while a browser view needs them. It
    acts on ROS only when a local user explicitly confirms it: sending and
    cancelling action goals, setting parameters through the standard
    parameter services, and calling services. It never publishes application
    data. Everything ROS-specific lives in ``ros_adapter``.

    SMPA is optional. ``smpa_adapter`` reads SMPA's recorded run directories
    read-only; nothing of SMPA is imported, and the ROS functionality works
    without it.

    The contract version names the JSON shapes documented in ``API.md``.
    Every HTTP response and every stream message follows that document.
    Version 1.3 is reserved for the SMPA conversation routes of GUI version 4,
    which are maintained as a separate patch.
"""

CONTRACT_VERSION = '1.4'
