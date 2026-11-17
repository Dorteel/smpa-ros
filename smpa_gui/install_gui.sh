#!/usr/bin/env bash
# Create smpa_gui/.venv and install the GUI's own Python packages into it; optionally build the
# ROS 2 package with colcon so that `ros2 run smpa_gui smpa_gui` and `ros2 launch smpa_gui
# gui.launch.py` use that environment.
# Nothing outside the smpa_gui/ directory is created or changed, except the workspace given to --build.
#   ./install_gui.sh                       runtime packages only
#   ./install_gui.sh --dev                 also the packages needed to run the tests
#   ./install_gui.sh --build ~/ros2_ws     also build smpa_gui into ~/ros2_ws/{build,install,log}
set -eo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fail() { echo "Error: $*" >&2; exit 1; }

requirements=requirements.txt
workspace=
while (( $# )); do
    case "$1" in
        --dev) requirements=requirements-dev.txt ;;
        --build) [[ -n "${2:-}" ]] || fail '--build needs a workspace directory'; workspace=$2; shift ;;
        *) fail "Unknown option: $1" ;;
    esac
    shift
done

# Use the ROS 2 environment that is already active, or source the installed one.
if ! python3 -c 'import rclpy' 2>/dev/null; then
    ros_setup="${SMPA_GUI_ROS_SETUP:-/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash}"
    [[ -f "$ros_setup" ]] || fail "ROS 2 setup not found: $ros_setup. Set SMPA_GUI_ROS_SETUP to your setup.bash."
    source "$ros_setup"
fi
python3 -c 'import rclpy, rosidl_runtime_py, numpy, PIL, yaml' \
    || fail 'The ROS 2 Python environment must provide rclpy, rosidl_runtime_py, NumPy, Pillow and PyYAML.'

# System site-packages make rclpy and the ROS message packages visible inside the environment.
[[ -x .venv/bin/python ]] || python3 -m venv --system-site-packages .venv \
    || fail 'Could not create smpa_gui/.venv (on Ubuntu: sudo apt install python3-venv).'
.venv/bin/python -m pip install --quiet -r "$requirements"
.venv/bin/python -c 'import fastapi, uvicorn, websockets, rclpy; print("GUI environment ready:", __import__("sys").prefix)'

if [[ -n "$workspace" ]]; then
    command -v colcon >/dev/null || fail 'colcon is not installed (on Ubuntu: sudo apt install python3-colcon-common-extensions).'
    mkdir -p "$workspace"
    workspace=$(cd -- "$workspace" && pwd)
    package=$PWD
    # Building with the GUI's interpreter makes the installed console script use it.
    (cd "$workspace" && "$package/.venv/bin/python" "$(command -v colcon)" --log-base "$workspace/log" build \
        --build-base "$workspace/build" --install-base "$workspace/install" \
        --paths "$package" --packages-select smpa_gui)
    echo "Built smpa_gui into $workspace/install. Use it with:"
    echo "  source $workspace/install/setup.bash && ros2 launch smpa_gui gui.launch.py"
fi
