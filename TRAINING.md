# Training the local policy

SFT and DPO are separate methods. LoRA is the trainable low-rank adapter; QLoRA
uses a 4-bit base with trainable LoRA. Neither is a separate RLHF algorithm.

## Valid examples

- **SFT:** original conversation context plus the human's corrected answer. TRL
  uses completion-only loss; user messages are not supervised targets.
- **DPO:** identical original context plus the explicitly chosen and rejected A/B
  answers. Identical candidates are rejected; regenerate them to obtain a pair.
- Thumbs, 1–5 scores and written justifications are metadata. They never become
  synthetic preference pairs or automatic target text.

Every saved feedback event remains immutable. Multiple corrections/preferences are
separate human judgements; inspect and select examples deliberately, especially if
judgements conflict. The dashboard previews the exact dataset before approval.

## Running an update

1. Select the base model in the sidebar. A small local Qwen3-0.6B is preferred.
2. Open Training. The default notification threshold is 10 **new** examples per
   method; reaching it only displays a notification. Manual batches can be smaller.
3. Choose SFT or DPO, select feedback IDs, and inspect the dataset preview.
4. Start a new adapter or explicitly choose to continue the sidebar adapter.
5. Review learning rate, epochs, batch size, accumulation, rank/alpha/dropout,
   maximum sequence length, precision and quantization. Continuing an adapter uses
   its saved rank/alpha/dropout; those fields configure only newly created adapters.
6. Check the explicit approval box, then press Start approved training.
7. Observe step metrics and status; Request stop cancels between steps. Model
   loading itself cannot be interrupted cooperatively. Refreshing or closing the
   browser does not terminate a worker.
8. On completion, choose the new checkpoint in the sidebar and Load model.

Training unloads the current browser session's inference model before launching.
Other sessions/processes may still occupy memory. Memory failures produce errors;
there is no silent switch of device, precision, model or quantization.

## Reference and incremental policy

For new LoRA DPO runs, TRL computes reference probabilities with the new adapter
disabled, so the unchanged base is the reference. For a continued DPO adapter, a
second frozen `reference` adapter contains the exact starting weights; TRL compares
against that reference while optimizing `default`. The reference adapter is not
published as the next policy. This follows TRL's
[DPO reference adapter support](https://huggingface.co/docs/trl/v0.24.0/en/dpo_trainer).
SFT uses TRL's
[prompt/completion dataset support](https://huggingface.co/docs/trl/v0.24.0/en/sft_trainer).

Each run has a fresh output directory and a saved dataset. Continuing means an
adapter warm start with a fresh optimizer/scheduler, not resuming optimizer state
or replaying an interrupted epoch. Previously successful example IDs are excluded
by method; checking Replay makes them selectable again. This is global to the vault
and method, rather than independent per experimental branch. Explicitly replay when
starting a separate branch that needs older examples.

Defaults: CPU float32, batch 1, accumulation 4, one epoch, LoRA rank 8, alpha 16,
dropout 0.05, learning rate 1e-4, max length 512. Sequences exceeding this limit are
subject to TRL truncation; choose a sufficient length after inspecting examples.
Long prompts may leave too little correction text; trainer errors remain visible.

CUDA can use float32, supported bfloat16, or float16. QLoRA additionally requires a
compatible bitsandbytes installation (optional `bitsandbytes==0.48.2`). CPU rejects
QLoRA and reduced precision. CUDA/QLoRA are implemented but need hardware testing.
No reward model or PPO is implemented. Decreasing training loss is not evidence of
better helpfulness; humans must compare responses and retain held-out examples.

Tests construct a tiny random GPT-2 locally and perform real optimizer steps for
both SFT and DPO, including continuation and adapter reload. This verifies plumbing,
not language capability, Qwen fine-tuning quality, or GPU memory fit.

## Package ownership

The implementation lives in `smpa_llm/smpa_llm/training/rlhf/`; the separate
pinned environment profile is `smpa_llm/requirements-rlhf.txt`. The root
requirements path and `run_rlhf.sh` remain compatible. Run from the repository:

```bash
./run_rlhf.sh
./test.sh --rlhf
```

The launcher supplies the source package path to the independent interpreter.
Approved workers execute `smpa_llm.training.rlhf.training` in that same environment.
Normal ROS inference never starts a trainer or imports training dependencies.
