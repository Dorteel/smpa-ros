# Known limitations and future work

Implemented scope: local Transformers inference, persistent conversation and all
four feedback types, explicit A/B choices, genuine TRL SFT/DPO with LoRA, adapter
continuation, approved worker execution, immutable checkpoints, and five views.

Verification limits:

- CUDA, mixed precision and bitsandbytes QLoRA need a compatible GPU test machine.
- Streamlit AppTest exercises widgets; a physical browser/layout check remains useful.
- Tiny random-model training verifies optimization, not qualitative improvement.
- No exhaustive compatibility guarantee for arbitrary architectures/chat templates.

Deliberately deferred: downloading models, advanced adapter registries, remote
multi-user authentication, cryptographic hashes of large base weights, optimizer
state resumption, held-out qualitative evaluations, reward models and PPO.

Operational limits: Linux/POSIX local storage, one app instance per vault, cooperative
stop between steps, no adapter checkpoint on cancellation, and no full browser
rendering automation. Keep original model directories unchanged. Do not edit
frontmatter while the application writes the same vault. A worker killed during
checkpoint saving can leave incomplete artifacts; only completed events advertise
usable checkpoints. Retrying requires a newly approved run and never overwrites it.
