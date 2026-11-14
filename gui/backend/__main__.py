"""Command-line entry point: ``python -m backend`` starts the GUI server."""
import argparse
import os

import uvicorn

from backend.app import create_app


def main(args=None):
    """Read the listening address and serve until interrupted.

    The address defaults to the loopback interface on port 8765 and can be
    set by arguments or the ``SMPA_GUI_HOST`` and ``SMPA_GUI_PORT`` variables.
    Binding to another interface exposes read access to ROS data to that
    network, so it must be requested explicitly; action goals are accepted
    from this computer only in any case. ``--read-only`` disables action
    execution altogether. Ctrl+C stops the web server, which asks the action
    servers to cancel goals that are still active (unless
    ``--keep-goals-on-exit`` is given) and then removes the gateway node
    from the ROS graph.
    """
    parser = argparse.ArgumentParser(description='Standalone ROS 2 GUI gateway: inspection plus explicit action goals.')
    parser.add_argument('--host', default=os.environ.get('SMPA_GUI_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('SMPA_GUI_PORT', '8765')))
    parser.add_argument('--read-only', action='store_true', default=os.environ.get('SMPA_GUI_READ_ONLY') == '1',
                        help='disable sending and cancelling action goals; inspection only')
    parser.add_argument('--keep-goals-on-exit', action='store_true',
                        default=os.environ.get('SMPA_GUI_KEEP_GOALS_ON_EXIT') == '1',
                        help='do not ask action servers to cancel active goals when the GUI stops')
    options = parser.parse_args(args)
    print(f'SMPA-ROS GUI: http://{options.host}:{options.port}'
          + (' (read-only)' if options.read_only else ' (action goals can be sent from this computer)'), flush=True)
    uvicorn.run(create_app(allow_actions=not options.read_only, cancel_on_exit=not options.keep_goals_on_exit),
                host=options.host, port=options.port, log_level='warning')


if __name__ == '__main__':
    main()
