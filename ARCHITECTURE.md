# Local RLHF Training Gym

This independent Python application is owned by `smpa_llm` and coexists with SMPA ROS.
The root `rlhf_gym` imports and app path remain compatibility entry points. It does not change
robot control flow, ROS services, model registry, or the existing default test mode.

The human workflow is direct:

```text
local base + optional adapter → conversation → human feedback
                                             ↓
                             correction (SFT) / A/B choice (DPO)
                                             ↓
                          select examples → explicit approval
                                             ↓
                            TRL worker → new adapter checkpoint
                                             ↓
                           reload adapter → conversation
```

- `smpa_llm/smpa_llm/training/rlhf/inference.py` loads local Transformers models, applies PEFT adapters,
  renders chat templates and generates responses. Original weights are never saved
  over. CPU uses float32; CUDA supports explicitly selected precision and QLoRA.
- `smpa_llm/smpa_llm/training/rlhf/vault.py` writes exclusive, atomic Markdown records with YAML
  frontmatter. Conversations are immutable headers plus ordered message events.
  Feedback snapshots the exact original prompt, generation settings and adapter.
- `smpa_llm/smpa_llm/training/rlhf/training.py` validates explicit approval and snapshots selected dataset
  rows. A subprocess runs TRL, freeing the dashboard to display progress and stop
  requests. A Linux advisory lock permits one training worker per vault. No trainer
  starts merely because feedback reaches a threshold.
- `smpa_llm/smpa_llm/training/rlhf/app.py` provides Conversation Gym, Training, Models, Experiments and
  Metrics. Streamlit browser memory holds only the loaded model and current UI
  selections. Durable history always comes from the vault.

## Storage

```text
RLHF-Vault/
  conversations/<id>.md       # stable conversation headers
  messages/<id>.md            # user messages, answers, comparison candidates
  feedback/<id>.md            # append-only judgements and corrections
  training/<id>.md            # approved config, exact dataset, feedback links
  metrics/<id>.md             # optimizer step, epoch and numeric metrics
  events/<id>.md              # status, error, stop or checkpoint reference
  artifacts/<run-id>/
    worker.log
    trainer/                 # framework output
    adapter/                 # completed PEFT weights and tokenizer
```

Frontmatter is authoritative; Markdown bodies offer readable renderings. Records
link through Obsidian wikilinks. Binary weights remain separate and checkpoint
references are relative to the vault. No database, model download service, cloud
service or container is needed.

A temporary file is flushed before atomic exclusive publication, then its directory
is flushed. Existing record identifiers cannot be overwritten. A user question is
saved before inference. Failed runs retain dataset/configuration and errors. An
absent running worker is shown as interrupted. Failed/cancelled runs do not consume
examples. Completed runs consume only their selected feedback IDs for their method;
replay requires explicit selection. Continuing an adapter preserves its LoRA
structure and writes a new child directory; its parent is never modified.

The app records library versions, full model configuration and a lightweight file
manifest. This does not hash multi-gigabyte weights: retain original local model
files unchanged for reproducibility. The deployment is intended for one trusted
human on loopback, with one application instance per vault. It has no remote-user
authentication or distributed locking.
