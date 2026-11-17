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
# Temporary Webots integration: reuse its interface package, never vendor a
# second package with the same ROS name. Explicit paths bypass unrelated copies.
simulation_source=${SIMULATION_ACTIONS_SOURCE:-$workspace/src/webots_ros2_simulation/simulation_actions}
[[ -f "$simulation_source/action/Look.action" && -f "$simulation_source/CMakeLists.txt" ]] || \
    fail 'Missing simulation_actions source. Clone Dorteel/webots_ros2_simulation (master) beside SMPA, or set SIMULATION_ACTIONS_SOURCE to its simulation_actions directory; see README.md.'
simulation_source=$(cd -- "$simulation_source" && pwd)
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
module = ast.parse(Path('smpa_ros/setup.py').read_text())
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
# declared inference dependencies. pip retains already satisfied versions.
# On a fresh CPU-only machine, avoid installing CUDA runtime wheels. Preserve
# any existing torch installation, including CUDA builds used by other workflows.
if ! .venv/bin/python -c 'import importlib.util; raise SystemExit(importlib.util.find_spec("torch") is None)'; then
    if ! command -v nvidia-smi >/dev/null || ! nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
        .venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision || \
            fail 'CPU PyTorch installation failed; check access to download.pytorch.org and rerun.'
    fi
fi
mapfile -t inference_requirements < <(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' requirements-inference.txt)
.venv/bin/python -m pip install -r "$PIP_CONSTRAINT" -r speech/requirements-speech.txt \
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
for filename in ('.venv/smpa-constraints.txt', 'speech/requirements-speech.txt'):
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
               'faster_whisper', 'piper.voice', 'sounddevice', 'av', 'openai', 'dotenv'):
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
.venv/bin/python - "$@" <<'PYMODELS'
from pathlib import Path
import sys
import yaml
from scripts.download_models import MODELS, run
from smpa_ros.model import unify_model, local_model_info, resolve_vision_model

root = Path.cwd()
pipeline = unify_model(yaml.safe_load((root / 'config.yaml').read_text()))
selected = pipeline['model']
backend = selected['backend']
known = {model[2]: model for model in MODELS}
required = set()
if backend == 'transformers':
    definition = dict(selected)
    if not definition.get('model_id') and not definition.get('path'):
        definition = dict(yaml.safe_load(
            (root / 'models' / (selected['model'] + '.yaml')).read_text()), **selected)
    model_id = definition.get('model_id')
    destination = definition.get('path')
    if destination:
        destination = Path(destination).expanduser()
        if not destination.is_absolute():
            destination = (root if destination.parts[0] == 'models' else root / 'models') / destination
        destination = destination.resolve()
    model = known.get(model_id)
    expected = root / 'models' / model[0] / model[1] if model else None
    if destination is not None and destination != expected:
        info = local_model_info(destination, vision=True, model_id=model_id, model_root=root / 'models')
        if info['status'] != 'complete':
            sys.exit(f"Configured model path {destination} is {info['status']}: {info['reason']}. "
                     'Provision that explicit path before installation, or select the downloader layout; '
                     'the installer will not replace an explicit model selection.')
        print(f'Using existing explicit local model: {destination}', flush=True)
    elif model:
        required.add(model_id)
    else:
        sys.exit(f'Configured model {model_id!r} is not supported by the downloader; '
                 'provide a complete explicit local model path.')
elif backend != 'api':
    sys.exit(f'Unsupported shared cognitive backend: {backend}; select transformers or api.')
selected_ids = set(known) if '--all-models' in sys.argv[1:] else required
if selected_ids:
    records = run(root=root / 'models', models=[model for model in MODELS if model[2] in selected_ids])
    incomplete = [model_id for model_id in sorted(selected_ids)
                  if records.get(model_id, {}).get('status') != 'completed']
    if incomplete:
        sys.exit(f'Requested models are incomplete: {incomplete}; inspect models/download_report.json and rerun.')
else:
    print('No model downloads required by the shared selection.')
if backend == 'transformers':
    resolved = resolve_vision_model(pipeline, root / 'models', root / 'models')
    print(f"Shared Sense/Model/Plan model verified: {resolved['path']}")
PYMODELS
# An older copy may have generated the existing package without Look. Reconfigure
# only when switching source directories; keep all build products at workspace level.
cmake_options=()
cache="$workspace/build/simulation_actions/CMakeCache.txt"
if [[ -f $cache ]] && ! grep -Fxq "CMAKE_HOME_DIRECTORY:INTERNAL=$simulation_source" "$cache"; then
    cmake_options+=(--cmake-clean-cache)
fi
# Explicit paths include both nested interface packages; no recursive discovery.
cd "$workspace"
"$repo/.venv/bin/python" "$(command -v colcon)" --log-base "$workspace/log" build \
    --build-base "$workspace/build" --install-base "$workspace/install" \
    --paths "$repo/smpa_ros" "$repo/smpa_llm" "$repo/smpa_ros_interfaces" "$simulation_source" \
    --packages-select simulation_actions smpa_ros_interfaces smpa_llm smpa_ros "${cmake_options[@]}"
# Report external readiness separately from successful environment/build setup.
source "$workspace/install/local_setup.bash"
cd "$repo"
"$repo/.venv/bin/python" - <<'PYREADY'
from pathlib import Path
import yaml
from smpa_ros.model import unify_model
pipeline = unify_model(yaml.safe_load(Path('config.yaml').read_text()))
print('Environment, selected downloads, and workspace build prepared.')
try:
    import simulation_actions.action as actions
    from rclpy.type_support import check_for_type_support
    for interface in (actions.Look, actions.LookAhead):
        check_for_type_support(interface)
    if set(actions.Look.Goal.get_fields_and_field_types()) != {'angle'}:
        raise RuntimeError('Unexpected Look goal fields; use the documented Webots master interface')
except Exception as error:
    raise SystemExit(f'Generated Webots action interfaces cannot be used: {error}. '
                     'Check workspace overlays and SIMULATION_ACTIONS_SOURCE, then rerun ./install.sh.')
print(f'Generated Webots actions verified: {actions.__file__}')
import torch
vision = pipeline['model']
if vision.get('backend') == 'api':
    print('API inference requires credentials in the environment or repository .env and endpoint access; no live API request was made.')
elif vision.get('device', 'auto') == 'cuda' and not torch.cuda.is_available():
    print('WARNING: vision.device=cuda is explicitly requested but CUDA is unavailable. '
          'Fix the driver/PyTorch installation or explicitly select cpu/auto before running.')
elif vision.get('device', 'auto') == 'cpu' or not torch.cuda.is_available():
    print('Vision will use CPU; auto precision is float32. Allow sufficient RAM and inference time.')
print('Start the simulation camera and Look action servers before ./run_smpa.sh; live endpoints were not checked.')
PYREADY
