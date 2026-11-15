#!/usr/bin/env bash
# Run the GUI's own tests. Requires ./install_gui.sh --dev.
#   ./test_gui.sh              unit tests and ROS integration tests (real fixture nodes and action servers)
#   ./test_gui.sh --browser    also drive the frontend in a headless browser; needs
#                              Playwright with Chromium in the interpreter named by
#                              SMPA_GUI_BROWSER_PYTHON (default: python3)
# The tests use a random ROS domain restricted to this computer.
set -eo pipefail
gui=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fail() { echo "Error: $*" >&2; exit 1; }
[[ -x "$gui/.venv/bin/python" ]] || fail 'gui/.venv is missing; run ./install_gui.sh --dev first.'
if ! "$gui/.venv/bin/python" -c 'import rclpy' 2>/dev/null; then
    ros_setup="${SMPA_GUI_ROS_SETUP:-/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash}"
    [[ -f "$ros_setup" ]] || fail "ROS 2 setup not found: $ros_setup."
    source "$ros_setup"
fi
"$gui/.venv/bin/python" -c 'import pytest, httpx' 2>/dev/null || fail 'Test packages are missing; run ./install_gui.sh --dev.'
cd "$gui"
"$gui/.venv/bin/python" -m pytest tests/test_units.py tests/test_frontend_logic.py tests/test_ros_integration.py tests/test_actions.py tests/test_smpa_runs.py tests/test_smpa_integration.py -v
if [[ "${1:-}" == --browser ]]; then
    GUI_TEST_PYTHON="$gui/.venv/bin/python" "${SMPA_GUI_BROWSER_PYTHON:-python3}" -m pytest tests/test_browser.py tests/test_browser_actions.py tests/test_browser_smpa.py -v
fi
