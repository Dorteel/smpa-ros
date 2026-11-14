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
    network, so it must be requested explicitly. Ctrl+C stops the web server,
    which in turn removes the gateway node from the ROS graph.
    """
    parser = argparse.ArgumentParser(description='Standalone observational ROS 2 GUI gateway.')
    parser.add_argument('--host', default=os.environ.get('SMPA_GUI_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('SMPA_GUI_PORT', '8765')))
    options = parser.parse_args(args)
    print(f'SMPA-ROS GUI: http://{options.host}:{options.port}', flush=True)
    uvicorn.run(create_app(), host=options.host, port=options.port, log_level='warning')


if __name__ == '__main__':
    main()
