#!/usr/bin/env bash
set -eo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mode="${1:---unit}"
case "$mode" in
    --unit) selector='not integration' ;;
    --integration) selector='integration' ;;
    --all) selector='integration or not integration' ;;
    *) echo 'Usage: ./test.sh [--unit|--integration|--all]' >&2; exit 2 ;;
esac
colcon_command=(colcon)
if [[ "$mode" != --unit ]]; then
    colcon_command=("$PWD/.venv/bin/python" "$(command -v colcon)")
fi

ros_setup="/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
if [[ ! -f "$ros_setup" ]]; then
    echo "ROS 2 setup not found: $ros_setup. Set ROS_DISTRO to your installed distribution." >&2
    exit 1
fi
source "$ros_setup"

# An isolated workspace prevents stale build products from masking failures.
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
export ROS_LOG_DIR="$work_dir/ros_logs"
"${colcon_command[@]}" --log-base "$work_dir/log" build --paths "$PWD" "$PWD/smpa_ros_interfaces" \
    --packages-select smpa_ros_interfaces smpa_ros --build-base "$work_dir/build" \
    --install-base "$work_dir/install"
source "$work_dir/install/setup.bash"
test_status=0
"${colcon_command[@]}" --log-base "$work_dir/log" test --paths "$PWD" "$PWD/smpa_ros_interfaces" \
    --packages-select smpa_ros --build-base "$work_dir/build" \
    --event-handlers console_direct+ \
    --python-testing pytest \
    --install-base "$work_dir/install" --return-code-on-test-failure \
    --pytest-args test -m "$selector" || test_status=$?
colcon test-result --test-result-base "$work_dir/build" --verbose
if (( test_status != 0 )); then
    exit "$test_status"
fi

# Run outside the source tree to verify that prompts/configs were installed.
export SMPA_TEST_PIPELINE_CONFIG="$PWD/config.yaml"
cd "$work_dir"
python3 - <<'PY'
import os
from importlib.resources import files
from importlib.metadata import distribution
import importlib.util
from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from runner import Runner
from memory import MemoryManager
import yaml

entries = {entry.name: entry for entry in distribution('smpa-ros').entry_points}
assert entries['smpa'].value == 'smpa_ros.smpa:main'
assert callable(entries['smpa'].load())
import inspect
assert Path(inspect.getfile(Runner)).parent == Path(inspect.getfile(MemoryManager)).parent
assert Path(inspect.getfile(Runner)).parent != Path(os.environ['SMPA_TEST_PIPELINE_CONFIG']).parent
for name, target in {'listen': 'speech.actions:listen_main', 'talk': 'speech.actions:talk_main',
                     'listener_talker': 'speech.demo:main'}.items():
    assert entries[name].value == target
    assert callable(entries[name].load())
assert importlib.util.find_spec('speech.observation') is not None
assert importlib.util.find_spec('smpa_ros.speech') is None
assert importlib.util.find_spec('smpa_ros.hello') is None
assert 'hello' not in entries
assert files('smpa_ros').joinpath('prompts/sense/describe_scene.txt').read_text().strip() == 'Describe the scene.'
registry = Path(get_package_share_directory('smpa_ros')) / 'models'
pipeline = yaml.safe_load((registry.parent / 'config.yaml').read_text())
assert pipeline == yaml.safe_load(Path(os.environ['SMPA_TEST_PIPELINE_CONFIG']).read_text())
assert type(pipeline['execution']['max_cycles']) is int and pipeline['execution']['max_cycles'] > 0
for name in ('smolvlm2', 'qwen3_vl', 'gemma3'):
    config = yaml.safe_load((registry / (name + '.yaml')).read_text())
    assert config['name'] == name and config['model_id'] and config['vram_gb'] is None

from smpa_ros.smpa import run
with Runner(root=Path.cwd() / 'memory', sense=lambda directory, cycle: 'observation',
            model=lambda prompt, **settings: '# World Model',
            plan=lambda prompt, **settings: '{"action":"look_left"}',
            act=lambda action, record: record.update(goal_accepted=True, result_status=4, success=True)) as runner:
    run(runner, 1)
    assert runner.memory.metadata['completed_cycles'] == 1

PY
timeout --kill-after=2s 10s ros2 run smpa_ros smpa --help > "$work_dir/smpa-help.txt"
grep -Fq -- '--max-cycles' "$work_dir/smpa-help.txt"
timeout --kill-after=2s 10s ros2 launch smpa_ros speech.launch.py --show-args > /dev/null
timeout --kill-after=2s 10s ros2 launch smpa_ros llm.launch.py --show-args > /dev/null
echo 'Build, tests (including SMPA cycles and audio persistence), installed assets, Speech entry points, and SMPA command checks passed.'
