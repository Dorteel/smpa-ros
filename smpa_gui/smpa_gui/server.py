"""Entry point of the ``smpa_gui`` ROS 2 package: start the GUI server and its ROS 2 node.

Concepts:
    One GUI process is one ROS 2 participant (the node ``/smpa_gui``) plus
    one local web server. ``ros2 run smpa_gui smpa_gui``, ``ros2 launch
    smpa_gui gui.launch.py``, ``python -m smpa_gui`` and ``run_gui.sh`` all
    end up in ``main`` below.

    The order of life is fixed: the web application is created, the web
    server starts, the application's start-up joins the ROS graph (rclpy
    context, node, executor thread), requests are served, and on Ctrl+C or
    SIGTERM the web server stops accepting requests, the application's
    shutdown settles active goals and leaves the ROS graph, and the process
    exits. The ROS context is private to the gateway, so rclpy never takes
    over the process signals that the web server relies on.

    SMPA integration is optional. Without it (``--no-smpa``) the SMPA tab
    reports that it is disabled, nothing of SMPA is read, and SMPA's
    conversation server is never started. With it, the SMPA layout
    (``--smpa-root``, ``--layout``) says where SMPA's checkout, interpreter,
    configuration and memory directory are; see ``smpa_adapter/smpa_layout.py``.
"""
import argparse
import os
from pathlib import Path

from smpa_gui.app import create_app


def strip_ros_arguments(arguments):
    """Remove the ``--ros-args ...`` block that ROS 2 tools may append to the command line.

    ``ros2 run`` and launch files add ROS-specific arguments after
    ``--ros-args``. They are meant for rclpy, not for this program's own
    options, so they are cut off before the options are parsed. Everything
    before ``--ros-args`` is returned unchanged.
    """
    arguments = list(arguments)
    return arguments[:arguments.index('--ros-args')] if '--ros-args' in arguments else arguments


def environment_flag(name):
    """Read a yes/no setting from an environment variable: '1', 'true' or 'yes' mean yes."""
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes')


def parse_options(arguments):
    """Turn the command line (and environment defaults) into the server's options.

    The address defaults to the loopback interface on port 8765 and can be
    set by arguments or the ``SMPA_GUI_HOST`` and ``SMPA_GUI_PORT``
    variables. ``--read-only`` disables every change the GUI could make:
    action goals, parameter changes and service calls. ``--keep-goals-on-exit``
    leaves active goals running when the GUI stops. ``--no-smpa`` disables
    the SMPA tab's data; ``--memory-root`` names SMPA's memory directory.
    """
    parser = argparse.ArgumentParser(
        prog='smpa_gui',
        description='Standalone ROS 2 GUI: inspection, plus explicitly confirmed action goals, parameter changes '
                    'and service calls from this computer.')
    parser.add_argument('--host', default=os.environ.get('SMPA_GUI_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('SMPA_GUI_PORT', '8765')))
    parser.add_argument('--read-only', action='store_true', default=environment_flag('SMPA_GUI_READ_ONLY'),
                        help='disable action goals, parameter changes and service calls; inspection only')
    parser.add_argument('--keep-goals-on-exit', action='store_true',
                        default=environment_flag('SMPA_GUI_KEEP_GOALS_ON_EXIT'),
                        help='do not ask action servers to cancel active goals when the GUI stops')
    parser.add_argument('--no-smpa', action='store_true', default=environment_flag('SMPA_GUI_NO_SMPA'),
                        help='disable the optional SMPA integration (the SMPA tab shows that it is disabled)')
    parser.add_argument('--memory-root', default=os.environ.get('SMPA_GUI_MEMORY_ROOT') or None,
                        help="SMPA's memory directory for the SMPA tab (read-only; default: <smpa root>/memory)")
    parser.add_argument('--smpa-root', default=os.environ.get('SMPA_GUI_SMPA_ROOT') or None,
                        help='SMPA checkout whose conversation server the Dialogue starts (default: the repository '
                             'containing this source checkout, if any)')
    parser.add_argument('--layout', default=os.environ.get('SMPA_GUI_LAYOUT') or None,
                        help='YAML file with SMPA paths for the Dialogue (see README.md)')
    return parser.parse_args(strip_ros_arguments(arguments))


def smpa_layout_for(options):
    """Load the SMPA layout with the command-line choices taking precedence over the environment."""
    from smpa_gui.smpa_adapter import smpa_layout
    environment = dict(os.environ)
    if options.smpa_root:
        environment['SMPA_GUI_SMPA_ROOT'] = options.smpa_root
    if options.memory_root:
        environment['SMPA_GUI_MEMORY_ROOT'] = options.memory_root
    return smpa_layout.load(options.layout, environment)


def smpa_memory_root(options):
    """Decide which SMPA memory directory the SMPA tab reads, or None.

    With SMPA disabled there is none. An explicitly given directory is used
    as it is. Otherwise the SMPA layout's memory directory is used when an
    SMPA checkout is known (given with ``--smpa-root`` or found around this
    source checkout). Failing that, ``memory/`` beside the package folder is
    used if it holds runs. An installed package has no such neighbour, so
    there the checkout or directory must be given explicitly.
    """
    if options.no_smpa:
        return None
    if options.memory_root:
        return options.memory_root
    layout = smpa_layout_for(options)
    if layout['smpa_root'] is not None:
        return str(layout['memory_root'])
    from smpa_gui.smpa_adapter.smpa_runs import default_root
    return default_root(Path(__file__).resolve().parent.parent)


def main(arguments=None):
    """Start the GUI server and serve until the process is asked to stop.

    Read the options, report the address, the change policy and the SMPA
    setting on the console, build the application and hand it to uvicorn.
    uvicorn installs the SIGINT and SIGTERM handlers; when one arrives it
    stops serving and runs the application's shutdown, which settles goals
    and leaves the ROS graph before the process exits. The interrupt that
    uvicorn raises again afterwards is the normal end, not an error.
    """
    import sys
    import uvicorn

    options = parse_options(sys.argv[1:] if arguments is None else arguments)
    memory_root = smpa_memory_root(options)
    layout = smpa_layout_for(options)
    if options.no_smpa:
        print('SMPA integration: disabled', flush=True)
    else:
        print(f'SMPA memory directory (read-only): {memory_root or "not configured"}', flush=True)
        print('SMPA conversation server: ' + (layout['conversation_url'] or (
            f'started on demand from {layout["smpa_root"]} on port {layout["conversation_port"]}'
            if layout['smpa_root'] else 'not configured (give --smpa-root or smpa_root:=)')), flush=True)
    print(f'SMPA-ROS GUI: http://{options.host}:{options.port}'
          + (' (read-only)' if options.read_only else
             ' (goals, parameter changes and service calls can be confirmed from this computer)'), flush=True)
    try:
        uvicorn.run(create_app(allow_changes=not options.read_only, cancel_on_exit=not options.keep_goals_on_exit,
                               memory_root=memory_root, smpa_enabled=not options.no_smpa, layout=layout),
                    host=options.host, port=options.port, log_level='warning')
    except KeyboardInterrupt:
        # uvicorn re-raises the interrupt after its graceful shutdown has completed.
        pass
