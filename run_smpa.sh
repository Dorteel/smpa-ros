#!/usr/bin/env bash
# Launch the prepared workspace; never build, install dependencies, or download.
set -eo pipefail
trap 'echo "SMPA failed at line $LINENO. See the error above and README.md troubleshooting." >&2' ERR
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace=$(cd -- "$repo/../.." && pwd)
fail() {
    # Report a startup precondition failure before any ROS participants are started.
    echo "Error: $*" >&2
    exit 1
}
# --all-senses is an explicit mode; existing ROS launch arguments remain valid.
read_session=''
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    echo 'Usage: ./run_smpa.sh [--all-senses CONVERSATION_RUN] [name:=value ...]'
    echo 'All-senses requires an existing conversation; it enables Hear, Read and Vision.'
    echo 'Use config:=<repo>/config/sense_observe.yaml for nonphysical verification.'
    exit 0
fi
if [[ ${1:-} == --all-senses ]]; then
    [[ $# -ge 2 && -n $2 && $2 != -* && $2 != *:=* ]] || fail '--all-senses requires a conversation run directory.'
    read_session=$(cd -- "$2" 2>/dev/null && pwd -P) || fail "Conversation directory does not exist: $2"
    shift 2
    for argument in "$@"; do
        case "$argument" in
            service:=*|hear:=*|vision:=*|read_session:=*|participants_only:=*)
                fail "--all-senses owns service, hear, vision, read_session and participants_only; remove $argument." ;;
        esac
    done
    set -- 'service:=all' 'hear:=true' 'vision:=true' "read_session:=$read_session" "$@"
fi
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
if [[ -n $read_session ]]; then
    # Inspect the installed package outside the checkout to avoid implicit checkout imports
    # concealing a stale installation. Validate Read without writing its source.
    (cd /; "$repo/.venv/bin/python" - "$read_session" <<'PYREAD'
from pathlib import Path
import sys
import yaml
try:
    from sense import modalities
    from sense.hear import observe as hear
    from sense.read import observe as read
    from sense.vision import SenseService
    from memory import MemoryManager
    directory, identity = MemoryManager.assigned_run(str(Path(sys.argv[1]).expanduser().resolve()))
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    if metadata.get('configuration', {}).get('kind') != 'conversation':
        raise ValueError('Read source must be a conversation run')
    if not (directory / 'conversation.md').is_file():
        raise ValueError('Conversation transcript is missing')
except Exception as error:
    raise SystemExit(f'Cannot start all sensing modalities: {error}. '
                     'Check the selected session and rebuild the current checkout with ./install.sh if needed.')
print(f'Read session: {directory} ({identity}); source remains read-only.', flush=True)
PYREAD
    )
fi
# Retain the repository as cwd for existing model and memory path defaults.
cd "$repo"
arguments=()
for default in 'service:=vision' 'max_cycles:=1' "config:=$repo/config.yaml" "python:=$repo/.venv/bin/python"; do
    overridden=false
    for argument in "$@"; do
        [[ $argument == "${default%%:=*}:="* ]] && overridden=true
    done
    if [[ $overridden == false ]]; then arguments+=("$default"); fi
done
exec ros2 launch smpa_ros llm.launch.py "${arguments[@]}" "$@"
