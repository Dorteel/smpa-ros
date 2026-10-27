#!/usr/bin/env bash
# Launch the prepared workspace; never build, install dependencies, or download.
set -eo pipefail
trap 'echo "SMPA failed at line $LINENO. See the error above and README.md troubleshooting." >&2' ERR
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace=$(cd -- "$repo/../.." && pwd)
fail() { echo "Error: $*" >&2; exit 1; }
[[ $(basename -- "$(dirname -- "$repo")") == src ]] || fail 'Place this checkout at <workspace>/src/smpa-ros.'
[[ -f /opt/ros/jazzy/setup.bash ]] || fail 'Install ROS 2 Jazzy first.'
source /opt/ros/jazzy/setup.bash
[[ -f "$workspace/install/setup.bash" ]] || fail 'Workspace installation is missing; run ./install.sh.'
# An old repository overlay must not be inherited or sourced through setup.bash.
for variable in AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH PATH; do
    case ":${!variable}:" in
        *":$repo/install/"*|*":$repo/install:"*|*":$repo/build/"*)
            fail 'A repository-level build is sourced. Open a fresh terminal without that overlay and run ./install.sh.' ;;
    esac
done
if grep -Fq -- "$repo/install" "$workspace/install/setup.bash"; then
    fail 'Workspace setup chains the stale repository overlay. Rebuild with ./install.sh from a fresh terminal.'
fi
source "$workspace/install/setup.bash"
[[ -x "$repo/.venv/bin/python" ]] || fail 'Missing .venv/bin/python; run ./install.sh.'
for package in smpa_ros smpa_ros_interfaces; do
    prefix=$(ros2 pkg prefix "$package" 2>/dev/null) || fail "Missing $package; run ./install.sh."
    [[ $prefix == "$workspace/install" || $prefix == "$workspace/install/$package" ]] || fail "$package resolves outside the workspace installation; run ./install.sh from a fresh terminal."
done
[[ -f "$prefix/share/smpa_ros_interfaces/package.xml" ]] || fail 'Incomplete interface installation; run ./install.sh.'
prefix=$(ros2 pkg prefix smpa_ros)
[[ -f "$prefix/share/smpa_ros/launch/llm.launch.py" ]] || fail 'Missing installed launch file; run ./install.sh.'
# Validate generated Python and native type support with the actual interpreter.
"$repo/.venv/bin/python" - <<'PY'
try:
    import simulation_actions.action as actions
    from rclpy.type_support import check_for_type_support
    check_for_type_support(actions.Look)
    if (set(actions.Look.Goal.get_fields_and_field_types()) != {'angle'}
            or set(actions.Look.Result.get_fields_and_field_types()) != {'success', 'message', 'achieved_angle'}
            or set(actions.Look.Feedback.get_fields_and_field_types()) != {'remaining_angle'}):
        raise RuntimeError('Look fields do not match the Webots master interface')
except Exception as error:
    raise SystemExit(f'Webots Look interface is missing, stale, or incompatible: {error}. '
                     'Run ./install.sh and source the correct workspace; see Temporary Webots Integration in README.md.')
print(f'Using generated Look interface: {actions.__file__}', flush=True)
PY
# Retain the repository as cwd for existing model and memory path defaults.
cd "$repo"
arguments=()
for default in 'service:=vision' 'max_cycles:=1' "config:=$repo/config/pipeline.yaml" "python:=$repo/.venv/bin/python"; do
    overridden=false
    for argument in "$@"; do
        [[ $argument == "${default%%:=*}:="* ]] && overridden=true
    done
    if [[ $overridden == false ]]; then arguments+=("$default"); fi
done
exec ros2 launch smpa_ros llm.launch.py "${arguments[@]}" "$@"
