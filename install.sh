#!/usr/bin/env bash
# Prepare the checkout environment and build at the enclosing ROS workspace root.
set -eo pipefail
trap 'echo "Installation failed at line $LINENO. Resolve the error above and rerun ./install.sh." >&2' ERR
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace=$(cd -- "$repo/../.." && pwd)
cd "$repo"
case "${1:-}" in
    '') [[ $# == 0 ]] || { echo 'Usage: ./install.sh [--all-models]' >&2; exit 2; } ;;
    --all-models) [[ $# == 1 ]] || { echo 'Usage: ./install.sh [--all-models]' >&2; exit 2; } ;;
    *) echo 'Usage: ./install.sh [--all-models]' >&2; exit 2 ;;
esac
fail() { echo "Error: $*" >&2; exit 1; }
[[ $(basename -- "$(dirname -- "$repo")") == src ]] || fail 'Place this checkout at <workspace>/src/smpa-ros.'
# Do not capture the obsolete repository overlay as a build underlay.
for variable in AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH PATH; do
    case ":${!variable}:" in
        *":$repo/install/"*|*":$repo/install:"*|*":$repo/build/"*)
            fail 'A repository-level build is sourced. Open a fresh terminal without that overlay, then rerun ./install.sh.' ;;
    esac
done
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || fail 'Ubuntu 24.04 is required.'
[[ -f /opt/ros/jazzy/setup.bash ]] || fail 'Install ROS 2 Jazzy first; see README.md.'
command -v colcon >/dev/null || fail 'Install python3-colcon-common-extensions first.'
/usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)' || fail 'System Python 3.12 is required.'
source /opt/ros/jazzy/setup.bash
/usr/bin/python3 -c 'import rclpy' || fail 'Install the ROS 2 Jazzy Python packages listed in README.md.'
if [[ ! -d .venv ]]; then
    /usr/bin/python3 -m venv --system-site-packages .venv || fail 'Install python3-venv, then rerun.'
fi
[[ -x .venv/bin/python ]] || fail 'The existing .venv is incomplete; repair or move it aside and rerun.'
.venv/bin/python -c 'import sys, rclpy; assert sys.version_info[:2] == (3, 12)' || fail 'The .venv must use Python 3.12 with system-site-packages for ROS.'
# Keep one dependency declaration in setup.py. The generated constraint file and
# pip configuration belong only to this virtual environment, not system Python.
.venv/bin/python - <<'PYDEPS'
import ast
from pathlib import Path
module = ast.parse(Path('setup.py').read_text())
setup_call = next(node.value for node in module.body
                  if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                  and isinstance(node.value.func, ast.Name) and node.value.func.id == 'setup')
requirements = ast.literal_eval(next(item.value for item in setup_call.keywords
                                    if item.arg == 'install_requires'))
Path('.venv/smpa-constraints.txt').write_text('\n'.join(requirements) + '\n')
PYDEPS
.venv/bin/python -m pip config --site set global.constraint "$repo/.venv/smpa-constraints.txt"
# Explicitly use the same constraints even if the invoking shell overrides pip config.
export PIP_CONSTRAINT="$repo/.venv/smpa-constraints.txt"
# Speech pins come from the existing declaration; vision requirements are the
# existing README inference dependencies. pip retains already satisfied versions.
# On a fresh CPU-only machine, avoid installing CUDA runtime wheels. Preserve
# any existing torch installation, including CUDA builds used by other workflows.
if ! .venv/bin/python -c 'import importlib.util; raise SystemExit(importlib.util.find_spec("torch") is None)'; then
    if ! command -v nvidia-smi >/dev/null || ! nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
        .venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision || \
            fail 'CPU PyTorch installation failed; check access to download.pytorch.org and rerun.'
    fi
fi
inference_requirements=(torch torchvision 'transformers>=4.57,<5' num2words pillow pyyaml huggingface_hub)
.venv/bin/python -m pip install -r "$PIP_CONSTRAINT" -r requirements-speech.txt \
    "${inference_requirements[@]}" || \
    fail 'Python dependency installation failed. Resolve the pip error above and rerun ./install.sh; do not bypass the virtual-environment constraints.'
# Validate SMPA's dependency graph, not unrelated packages visible through ROS.
.venv/bin/python - "${inference_requirements[@]}" <<'PYVERIFY'
import importlib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import sys
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

requirements = list(sys.argv[1:]) + ['colcon-core', 'rclpy']
for filename in ('.venv/smpa-constraints.txt', 'requirements-speech.txt'):
    requirements.extend(line.split('#', 1)[0].strip()
                        for line in Path(filename).read_text().splitlines()
                        if line.split('#', 1)[0].strip())
pending = [(Requirement(value), 'SMPA') for value in requirements]
checked = set()
errors = []
while pending:
    requirement, parent = pending.pop()
    try:
        installed = distribution(requirement.name)
    except PackageNotFoundError:
        errors.append(f'{parent} requires {requirement}, but it is missing')
        continue
    if requirement.specifier and not requirement.specifier.contains(installed.version, prereleases=True):
        errors.append(f'{parent} requires {requirement}, but {installed.version} is installed')
    key = (canonicalize_name(requirement.name), tuple(sorted(requirement.extras)))
    if key in checked:
        continue
    checked.add(key)
    # Check dependencies of required packages, including their requested extras.
    # Reverse dependencies (e.g. unrelated Flask/Jupyter consumers) are excluded.
    for value in installed.requires or []:
        dependency = Requirement(value)
        if dependency.marker is None or any(
                dependency.marker.evaluate({'extra': extra})
                for extra in ('', *requirement.extras)):
            pending.append((dependency, installed.metadata['Name']))

for module in ('setuptools', 'rclpy', 'colcon_core', 'torch', 'torchvision',
               'transformers', 'num2words', 'PIL.Image', 'yaml', 'huggingface_hub',
               'faster_whisper', 'piper.voice', 'sounddevice', 'av'):
    try:
        importlib.import_module(module)
    except Exception as error:
        hint = ('; install the documented system prerequisite with sudo apt install libportaudio2'
                if module == 'sounddevice' and 'PortAudio' in str(error) else '')
        errors.append(f'Cannot import {module}: {error}{hint}')
try:
    from transformers import AutoModelForCausalLM, AutoTokenizer, AutoModelForImageTextToText, AutoProcessor
    from outlines import from_transformers
    from outlines.backends import get_json_schema_logits_processor
    from jsonschema.validators import validator_for
except Exception as error:
    errors.append(f'Cannot import required inference APIs: {error}')
if errors:
    sys.exit('SMPA dependency verification failed:\n- ' + '\n- '.join(errors)
             + '\nResolve these required-package conflicts before rerunning ./install.sh. '
               'Do not bypass the setuptools constraint or change system Python.')
print('SMPA dependencies, required APIs, and ROS/colcon version constraints verified.')
print('Optional global diagnostic: .venv/bin/python -m pip check '
      '(also reports unrelated system-package conflicts).')
PYVERIFY
.venv/bin/python - "$@" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
import yaml
from scripts.download_models import MODELS

root = Path.cwd()
pipeline = yaml.safe_load((root / 'config/pipeline.yaml').read_text())
registry = yaml.safe_load((root / 'models/model_library.yaml').read_text())['models']
text = registry[pipeline['modeling']['model']]
vision = yaml.safe_load((root / 'models' / (pipeline['model'] + '.yaml')).read_text())
# Default execution uses vision, whose existing backend always needs local weights.
required = {vision['model_id']}
if text['backend'] == 'transformers':
    required.add(text['model_id'])
elif text['backend'] == 'ollama':
    print(f"Ollama text model: {text['model_id']}; no text weights will be downloaded.", flush=True)
else:
    sys.exit(f"Unsupported existing modeling backend: {text['backend']}")
known = {model[2]: model for model in MODELS}
if required - known.keys():
    sys.exit(f'Configured models are not supported by the existing downloader: {required - known.keys()}')
if text['backend'] == 'transformers':
    model = known[text['model_id']]
    if Path(text['path']) != Path(model[0]) / model[1]:
        sys.exit('The configured text model path does not match the downloader layout.')
selected = list(known) if '--all-models' in sys.argv[1:] else sorted(required)
command = [sys.executable, str(root / 'scripts/download_models.py')]
for model_id in selected:
    command.extend(['--model', model_id])
subprocess.run(command, check=True)
report = json.loads((root / 'models/download_report.json').read_text())
completed = {r['repo_id'] for r in report['models'] if r['status'] == 'completed'}
if set(selected) - completed:
    sys.exit('Some requested models are incomplete; inspect models/download_report.json and rerun.')
PY
# Explicit paths include the nested interface package. Colcon reuses build state.
cd "$workspace"
colcon --log-base "$workspace/log" build \
    --build-base "$workspace/build" --install-base "$workspace/install" \
    --paths "$repo" "$repo/smpa_ros_interfaces" \
    --packages-select smpa_ros_interfaces smpa_ros
# Report external readiness separately from successful environment/build setup.
source "$workspace/install/local_setup.bash"
cd "$repo"
"$repo/.venv/bin/python" - <<'PY'
from pathlib import Path
import yaml
pipeline = yaml.safe_load(Path('config/pipeline.yaml').read_text())
registry = yaml.safe_load(Path('models/model_library.yaml').read_text())['models']
backend = registry[pipeline['modeling']['model']]['backend']
print('Environment, selected downloads, and workspace build prepared.')
if backend != 'transformers':
    print('WARNING: Ollama is supported for text generation, but the existing full SMPA loop rejects '
          'Ollama at schema-constrained Plan. A full run requires a configured Transformers model '
          'and Outlines. Provision the Ollama server/model separately for text-only use.')
try:
    from simulation_actions.action import Look
except (ImportError, OSError) as error:
    print(f'WARNING: Simulation interface unavailable ({error}); build/source its workspace before running.')
import torch
vision = pipeline.get('vision', {})
if vision.get('device', 'auto') == 'cuda' and not torch.cuda.is_available():
    print('WARNING: vision.device=cuda is explicitly requested but CUDA is unavailable. '
          'Fix the driver/PyTorch installation or explicitly select cpu/auto before running.')
elif vision.get('device', 'auto') == 'cpu' or not torch.cuda.is_available():
    print('Vision will use CPU; auto precision is float32. Allow sufficient RAM and inference time.')
print('Start the simulation camera and Look action servers before ./run_smpa.sh; live endpoints were not checked.')
PY
