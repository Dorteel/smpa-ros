#!/usr/bin/env bash
# Create gui/.venv and install the GUI's own Python packages into it.
# Nothing outside the gui/ directory is created or changed.
#   ./install_gui.sh         runtime packages only
#   ./install_gui.sh --dev   also the packages needed to run the tests
set -eo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fail() { echo "Error: $*" >&2; exit 1; }

# Use the ROS 2 environment that is already active, or source the installed one.
if ! python3 -c 'import rclpy' 2>/dev/null; then
    ros_setup="${SMPA_GUI_ROS_SETUP:-/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash}"
    [[ -f "$ros_setup" ]] || fail "ROS 2 setup not found: $ros_setup. Set SMPA_GUI_ROS_SETUP to your setup.bash."
    source "$ros_setup"
fi
python3 -c 'import rclpy, rosidl_runtime_py, numpy, PIL, yaml' \
    || fail 'The ROS 2 Python environment must provide rclpy, rosidl_runtime_py, NumPy, Pillow and PyYAML.'

requirements=requirements.txt
[[ "${1:-}" == --dev ]] && requirements=requirements-dev.txt
# System site-packages make rclpy and the ROS message packages visible inside the environment.
[[ -x .venv/bin/python ]] || python3 -m venv --system-site-packages .venv \
    || fail 'Could not create gui/.venv (on Ubuntu: sudo apt install python3-venv).'
.venv/bin/python -m pip install --quiet -r "$requirements"
.venv/bin/python -c 'import fastapi, uvicorn, websockets, rclpy; print("GUI environment ready:", __import__("sys").prefix)'
