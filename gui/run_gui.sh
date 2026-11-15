#!/usr/bin/env bash
# Start the standalone SMPA-ROS GUI. This launcher never installs or builds anything.
#   ./run_gui.sh [--host 127.0.0.1] [--port 8765]
# Environment:
#   SMPA_GUI_ROS_SETUP  ROS 2 setup.bash to source (default: /opt/ros/$ROS_DISTRO or jazzy)
#   SMPA_GUI_OVERLAY    workspace install/setup.bash providing custom message types,
#                       or "none" (default: the enclosing colcon workspace, if any)
#   SMPA_GUI_HOST, SMPA_GUI_PORT  listening address (default 127.0.0.1:8765)
#   SMPA_GUI_MEMORY_ROOT  SMPA memory directory shown in the SMPA tab, read-only
#                         (default: memory/ beside gui/, if it exists)
set -eo pipefail
gui=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fail() { echo "Error: $*" >&2; exit 1; }
[[ -x "$gui/.venv/bin/python" ]] || fail 'gui/.venv is missing; run ./install_gui.sh first.'

# Use the ROS 2 environment that is already active, or source the installed one.
if ! "$gui/.venv/bin/python" -c 'import rclpy' 2>/dev/null; then
    ros_setup="${SMPA_GUI_ROS_SETUP:-/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash}"
    [[ -f "$ros_setup" ]] || fail "ROS 2 setup not found: $ros_setup. Set SMPA_GUI_ROS_SETUP to your setup.bash."
    source "$ros_setup"
fi

# Custom message types (for example SMPA's own interfaces) can only be shown when the
# workspace that defines them is sourced. Look for <workspace>/install beside a src/ parent.
overlay="${SMPA_GUI_OVERLAY:-}"
if [[ -z "$overlay" ]]; then
    directory=$gui
    while [[ "$directory" != / ]]; do
        if [[ $(basename -- "$directory") == src && -f "$(dirname -- "$directory")/install/setup.bash" ]]; then
            overlay="$(dirname -- "$directory")/install/setup.bash"
            break
        fi
        directory=$(dirname -- "$directory")
    done
fi
if [[ -n "$overlay" && "$overlay" != none ]]; then
    [[ -f "$overlay" ]] || fail "Workspace overlay not found: $overlay"
    source "$overlay"
    echo "Using workspace overlay: $overlay"
else
    echo 'No workspace overlay sourced; topics with custom message types are listed but cannot be displayed.'
fi

cd "$gui"
exec "$gui/.venv/bin/python" -m backend "$@"
