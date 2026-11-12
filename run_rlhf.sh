#!/usr/bin/env bash
# Launch only: never install, download models, or start training here.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
exec "${RLHF_PYTHON:-$PWD/.venv-rlhf/bin/python}" -m streamlit run rlhf_gym/app.py --server.address 127.0.0.1 --server.port "${RLHF_PORT:-8501}" --server.headless true --browser.gatherUsageStats false
