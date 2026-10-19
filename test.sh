#!/usr/bin/env bash
set -eo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mode="${1:---unit}"
pytest_options=()
case "$mode" in
    --unit) selector='not integration' ;;
    --integration) selector='integration' ;;
    --all) selector='integration or not integration' ;;
    *) echo 'Usage: ./test.sh [--unit|--integration|--all]' >&2; exit 2 ;;
esac
colcon_command=(colcon)
if [[ "$mode" != --unit ]]; then
    # Real inference uses the existing optional-dependency environment.
    colcon_command=("$PWD/.venv/bin/python" "$(command -v colcon)")
    pytest_options=(-s)
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
    --pytest-args test -m "$selector" "${pytest_options[@]}" || test_status=$?
colcon test-result --test-result-base "$work_dir/build" --verbose
if (( test_status != 0 )); then
    exit "$test_status"
fi

# Run outside the source tree to verify that prompts/configs were installed.
export SMPA_TEST_PIPELINE_CONFIG="$PWD/config/pipeline.yaml"
cd "$work_dir"
python3 - <<'PY'
import os
from importlib.resources import files
from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from smpa_ros.loop import run_cycle
import yaml

assert files('smpa_ros.sense').joinpath('prompts/describe_scene.txt').read_text().strip() == 'Describe the scene.'
registry = Path(get_package_share_directory('smpa_ros')) / 'models'
pipeline = yaml.safe_load((registry.parent / 'config/pipeline.yaml').read_text())
assert pipeline == yaml.safe_load(Path(os.environ['SMPA_TEST_PIPELINE_CONFIG']).read_text())
for name in ('smolvlm2', 'qwen3_vl', 'gemma3'):
    config = yaml.safe_load((registry / (name + '.yaml')).read_text())
    assert config['name'] == name and config['model_id'] and config['vram_gb'] is None

class Echo:
    def generate(self, prompt, *, max_new_tokens=128):
        return prompt

backend = Echo()
assert list(run_cycle('test observation', sense=backend, model=backend,
                      plan=backend, act=backend)) == ['sense', 'model', 'plan', 'act']
PY
# The node exits itself; timeout terminates and reaps it if it hangs.
timeout --kill-after=2s 10s ros2 run smpa_ros hello > "$work_dir/hello.log" 2>&1
cat "$work_dir/hello.log"
grep -Fq 'Hello ROS 2!' "$work_dir/hello.log"
timeout --kill-after=2s 10s ros2 run smpa_ros loop --help > /dev/null
timeout --kill-after=2s 10s ros2 launch smpa_ros llm.launch.py --show-args > /dev/null
echo 'Build, tests, installed assets, hello output, and loop command checks passed.'
