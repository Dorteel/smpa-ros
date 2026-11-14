#!/usr/bin/env bash
set -eo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mode="${1:---unit}"
if [[ "$mode" == --rlhf ]]; then
    export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
    export TOKENIZERS_PARALLELISM=false WANDB_DISABLED=true
    rlhf_test_dir="$(mktemp -d)"
    export HF_HOME="$rlhf_test_dir/hf"
    trap 'rm -rf -- "$rlhf_test_dir"' EXIT
    gym_python="${RLHF_PYTHON:-$PWD/.venv-rlhf/bin/python}"
    "$gym_python" -c 'import torch, transformers, peft, trl, accelerate, datasets, streamlit, yaml; print("Gym dependencies loaded; CUDA:", torch.cuda.is_available())'
    exec_status=0
    "$gym_python" -m pytest -c /dev/null tests_rlhf -v --basetemp "$rlhf_test_dir/pytest" || exec_status=$?
    exit "$exec_status"
fi
case "$mode" in
    --unit) selector='not integration' ;;
    --integration) selector='integration' ;;
    --all) selector='integration or not integration' ;;
    *) echo 'Usage: ./test.sh [--unit|--integration|--all|--rlhf]' >&2; exit 2 ;;
esac
# Use the prepared environment for API imports in unit tests as well.
test_python=$(command -v python3)
if [[ -x "$PWD/.venv/bin/python" || "$mode" != --unit ]]; then
    test_python="$PWD/.venv/bin/python"
fi
colcon_command=("$test_python" "$(command -v colcon)")

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
    --pytest-args test -m "$selector" -ra || test_status=$?
colcon test-result --test-result-base "$work_dir/build" --verbose
if (( test_status != 0 )); then
    exit "$test_status"
fi

# Colcon accepts pytest's no-tests exit code. Require actual test execution before
# reporting success, including when collection was skipped for missing dependencies.
"$test_python" - "$work_dir/build/smpa_ros/pytest.xml" <<'PY'
import sys
from xml.etree import ElementTree

cases = ElementTree.parse(sys.argv[1]).getroot().iter('testcase')
if not any(case.find('skipped') is None for case in cases):
    raise SystemExit('No ROS tests executed; inspect the collection and skip reasons above.')
PY

# Run outside the source tree to verify that prompts/configs were installed.
export SMPA_TEST_PIPELINE_CONFIG="$PWD/config.yaml"
cd "$work_dir"
"$test_python" - <<'PY'
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
assert entries['conversation'].value == 'smpa_ros.conversation_web:main'
assert callable(entries['conversation'].load())
assert b'Belief graph' in files('smpa_ros').joinpath('conversation.html').read_bytes()
from smpa_ros.belief import ontology_graph
ontology, digest = ontology_graph()
assert len(ontology) > 0 and len(digest) == 64
for name, target in {'visual_jev': 'smpa_ros.visual_jev.service:main',
                     'visual_jev_camera': 'smpa_ros.visual_jev.camera:main'}.items():
    assert entries[name].value == target
    assert callable(entries[name].load())
from smpa_ros_interfaces.srv import EvaluateVisualDecision
from rclpy.type_support import check_for_type_support
check_for_type_support(EvaluateVisualDecision)
assert set(EvaluateVisualDecision.Response.get_fields_and_field_types()) == {
    'success', 'selected_answer', 'answer_labels', 'probabilities', 'error_message'}
assert entries['segment_image'].value == 'smpa_ros.segmentation:main'
assert callable(entries['segment_image'].load())
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
timeout --kill-after=2s 10s ros2 run smpa_ros visual_jev_camera --help > /dev/null
echo 'Build, tests (including SMPA cycles and audio persistence), installed assets, Speech entry points, and SMPA command checks passed.'
