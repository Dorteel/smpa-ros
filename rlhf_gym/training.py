"""Turn approved, reproducible datasets into immutable PEFT checkpoints.

Concepts: approval is an explicit action, never a consequence of a feedback count.
A run is an immutable input snapshot. Its status and metrics are additional events.
The worker owns one training lock; cancellation is cooperative at training steps.
For DPO, the reference is frozen at the starting policy, including a continued
adapter, so incremental updates compare against the actual policy they start from.
"""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import traceback

from rlhf_gym import vault
from transformers import TrainerCallback

DEFAULTS = {'algorithm': 'SFT', 'learning_rate': 0.0001, 'epochs': 1.0, 'batch_size': 1,
            'gradient_accumulation': 4, 'rank': 8, 'alpha': 16, 'dropout': 0.05,
            'precision': 'float32', 'quantization': False, 'device': 'cpu',
            'max_length': 512, 'seed': 42, 'max_steps': -1, 'adapter': None}


def run_status(root, run_id):
    """Recover the latest run status from append-only events.

    Runs without status events are approved but not yet started. A running worker
    whose process has disappeared is reported as interrupted, making crashes visible
    without deleting its inputs or treating its examples as successfully consumed.
    """
    events = [event for event in vault.records(root, 'events') if event.get('run_id') == run_id and 'status' in event]
    status = events[-1] if events else {'status': 'approved'}
    if status['status'] in {'queued', 'running'}:
        try:
            process_state = Path(f"/proc/{status['pid']}/stat").read_text().rsplit(') ', 1)[1].split()
            alive = process_state[0] != 'Z' and process_state[19] == status.get('process_start')
        except FileNotFoundError:
            alive = False
        if not alive:
            return dict(status, status='interrupted', error='Worker exited without completion; inputs and partial artifacts remain saved. Inspect worker.log.')
    return status


def approve_run(root, configuration, selected_ids, *, approved=False, replay=False):
    """Snapshot selected valid examples only after explicit human approval.

    Merge conservative defaults with submitted parameters, validate the method and
    positive training controls, and reconstruct eligible examples from Markdown.
    Reject missing, already consumed, or incompatible feedback. Record both dataset
    rows and provenance before returning a run. This operation does not start a worker.
    """
    if approved is not True:
        raise PermissionError('Explicit training approval is required')
    config = dict(DEFAULTS, **configuration)
    if config['algorithm'] not in {'SFT', 'DPO'}:
        raise ValueError('Unknown training method')
    if not Path(config['model']).is_dir():
        raise ValueError('Choose an existing local base model')
    config['model'] = str(Path(config['model']).resolve())
    if config.get('adapter'):
        config['adapter'] = str(Path(config['adapter']).resolve())
        if not (Path(config['adapter']) / 'adapter_config.json').is_file():
            raise ValueError('Choose a saved adapter checkpoint to continue')
    for field in ('learning_rate', 'epochs', 'batch_size', 'gradient_accumulation', 'rank', 'alpha', 'max_length'):
        if config[field] <= 0:
            raise ValueError(f'{field} must be positive')
    if not 0 <= config['dropout'] < 1:
        raise ValueError('LoRA dropout must be in [0, 1)')
    if config['max_length'] < 8 or config['max_steps'] == 0:
        raise ValueError('Use max_length >= 8 and max_steps -1 or a positive count')
    eligible = {entry['id']: entry['row'] for entry in vault.examples(root, config['algorithm'], config['model'], replay)}
    if not selected_ids or len(set(selected_ids)) != len(selected_ids) or any(key not in eligible for key in selected_ids):
        raise ValueError('Select unique eligible examples for this method and model')
    from rlhf_gym.inference import model_information, hardware
    return vault.append(root, 'training', artifacts=model_information(config['model'], config['adapter']),
                        hardware=hardware(), title=f"{config['algorithm']} training", algorithm=config['algorithm'],
                        configuration=config, example_ids=list(selected_ids),
                        dataset=[eligible[key] for key in selected_ids], approved=True, replay=replay,
                        feedback_links=[f'[[feedback/{key}]]' for key in selected_ids])


def start_worker(root, run):
    """Launch one approved run in an independent Python process.

    Recover the run from disk rather than trusting a supplied approval flag. A
    per-run launch lock rejects duplicate starts; the global worker lock prevents
    competing jobs. Save stdout and stderr beside artifacts for failure diagnosis.
    The returned process identifier is informational; status is recovered from events.
    """
    persisted = next((item for item in vault.records(root, 'training') if item['id'] == run['id']), None)
    if not persisted or persisted.get('approved') is not True:
        raise PermissionError('No persisted approval exists')
    root = Path(root).resolve()
    directory = root / 'artifacts' / run['id']
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (directory / 'launched').exists():
            raise ValueError('This approved run has already been launched; approve a new run to retry')
        (directory / 'launched').touch(exist_ok=False)
        with (directory / 'worker.log').open('a') as output:
            process = subprocess.Popen([sys.executable, '-m', 'rlhf_gym.training', '--vault', str(root), '--run', run['id']],
                                       cwd=Path(__file__).resolve().parent.parent, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            # Publish queued state before releasing the launch lock, which the worker
            # also acquires before checking whether it has already started.
            process_start = Path(f'/proc/{process.pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
            vault.append(root, 'events', run_id=run['id'], status='queued', pid=process.pid, process_start=process_start)
    return process.pid


def request_stop(root, run_id):
    """Record a cooperative stop request for a running or queued job.

    The worker checks this append-only event between training steps. No checkpoint
    is advertised as completed after cancellation; partial artifacts remain inspectable.
    """
    return vault.append(root, 'events', run_id=run_id, action='stop_requested')


def execute(root, run_id):
    """Train one approved snapshot and record every terminal outcome.

    Validate approval before importing trainers or allocating weights. Acquire an
    exclusive vault training lock and reject a run already started. Load the local
    base and optional continued adapter; create LoRA only for a new adapter. Build
    the appropriate TRL dataset and frozen DPO reference, then train while appending
    metrics. Save the adapter in a fresh directory, then publish completion. Errors
    and cancellation leave the dataset and diagnostic events intact without consuming
    examples or replacing any checkpoint. Release the worker lock on every outcome.
    """
    root = Path(root).resolve()
    run = next((item for item in vault.records(root, 'training') if item['id'] == run_id), None)
    if not run or run.get('approved') is not True:
        raise PermissionError('Training requires a persisted approved run')
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.training.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            vault.append(root, 'events', run_id=run_id, status='failed', error='Another training worker owns this vault')
            return
        directory = root / 'artifacts' / run_id
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'launch.lock').open('a') as launch_lock:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)
            if run_status(root, run_id)['status'] not in {'approved', 'queued'}:
                raise ValueError('Run has already started')
            process_start = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(') ', 1)[1].split()[19]
            vault.append(root, 'events', run_id=run_id, status='running', pid=os.getpid(), process_start=process_start)
        try:
            _train(root, run)
        except Exception as error:
            vault.append(root, 'events', run_id=run_id, status='failed', error=str(error), traceback=traceback.format_exc())
            raise


def _train(root, run):
    """Apply the selected TRL algorithm to the run's saved dataset.

    SFT uses completion-only loss on corrected answers. DPO keeps the chosen and
    rejected continuations and uses the frozen starting policy as reference. A
    continued adapter supplies its saved LoRA structure instead of being recreated.
    Step callbacks append metrics and observe stop requests. Only a finished run
    publishes a checkpoint reference; cancellation preserves partial work separately.
    """
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import TrainerCallback, set_seed
    from trl import SFTConfig, SFTTrainer, DPOConfig, DPOTrainer
    from rlhf_gym.inference import load_model
    config = run['configuration']
    set_seed(config['seed'])
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    model, tokenizer = load_model(config['model'], config['device'], config['adapter'],
                                  config['precision'], config['quantization'], trainable=True)
    model.config.use_cache = False
    if config['adapter']:
        if config['algorithm'] == 'DPO':
            model.load_adapter(config['adapter'], adapter_name='reference', is_trainable=False)
            model.set_adapter('default')
    else:
        lora = LoraConfig(r=config['rank'], lora_alpha=config['alpha'], lora_dropout=config['dropout'],
                          task_type='CAUSAL_LM', target_modules='all-linear')
        model = get_peft_model(model, lora)
    directory = root / 'artifacts' / run['id']
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint = directory / 'adapter'
    if checkpoint.exists():
        raise ValueError('Checkpoint directory already exists; refusing to overwrite it')
    common = dict(output_dir=str(directory / 'trainer'), learning_rate=config['learning_rate'],
                  num_train_epochs=config['epochs'], per_device_train_batch_size=config['batch_size'],
                  gradient_accumulation_steps=config['gradient_accumulation'], max_steps=config['max_steps'],
                  bf16=config['precision'] == 'bfloat16', fp16=config['precision'] == 'float16',
                  use_cpu=config['device'] == 'cpu', logging_steps=1, save_strategy='no',
                  report_to='none', seed=config['seed'], gradient_checkpointing=False,
                  dataloader_pin_memory=False, optim='adamw_torch', remove_unused_columns=True)
    callback = Progress(root, run['id'])
    dataset = Dataset.from_list(run['dataset'])
    if config['algorithm'] == 'SFT':
        args = SFTConfig(**common, max_length=config['max_length'], completion_only_loss=True, packing=False)
        trainer = SFTTrainer(model=model, args=args, train_dataset=dataset, processing_class=tokenizer, callbacks=[callback])
    else:
        reference_options = {}
        if config['adapter']:
            reference_options = {'model_adapter_name': 'default', 'ref_adapter_name': 'reference'}
        args = DPOConfig(**common, max_length=config['max_length'], max_prompt_length=config['max_length'] // 2,
                         beta=0.1, **reference_options)
        trainer = DPOTrainer(model=model, ref_model=None, args=args, train_dataset=dataset,
                             processing_class=tokenizer, callbacks=[callback])
    callback.on_train_begin(trainer.args, trainer.state, trainer.control)
    if callback.stopped:
        vault.append(root, 'events', run_id=run['id'], status='cancelled')
        return
    trainer.train()
    if callback.stopped:
        vault.append(root, 'events', run_id=run['id'], status='cancelled')
        return
    checkpoint.mkdir(exist_ok=False)
    model.save_pretrained(checkpoint, selected_adapters=['default'])
    tokenizer.save_pretrained(checkpoint)
    vault.append(root, 'events', run_id=run['id'], status='completed',
                 checkpoint=str(checkpoint.relative_to(root)), parent_adapter=config['adapter'],
                 base_model=config['model'], steps=trainer.state.global_step)


class Progress(TrainerCallback):
    """Append training measurements and honor a human's cooperative stop request."""

    def __init__(self, root, run_id):
        """Bind this callback to the vault and the approved run being observed.

        Keep cancellation state locally while persistent events remain authoritative.
        No optimization settings or training examples are changed by the observer.
        """
        self.root = root
        self.run_id = run_id
        self.stopped = False

    def on_log(self, args, state, control, logs=None, **kwargs):
        """Append numeric trainer logs with their optimizer step and epoch.

        Keep ordinary numeric values that serialize as YAML. Every measurement is
        independent and remains readable if optimization later fails.
        """
        values = {key: float(value) for key, value in (logs or {}).items() if isinstance(value, (int, float))}
        vault.append(self.root, 'metrics', run_id=self.run_id, step=state.global_step, epoch=state.epoch, values=values)

    def on_step_end(self, args, state, control, **kwargs):
        """Stop after the current optimizer step when a human request exists.

        Preserve the local stopped flag so the worker reports cancellation instead
        of publishing a completed checkpoint. Return the framework's control state.
        """
        self.stopped = any(item.get('run_id') == self.run_id and item.get('action') == 'stop_requested'
                           for item in vault.records(self.root, 'events'))
        control.should_training_stop = self.stopped
        return control

    def on_train_begin(self, args, state, control, **kwargs):
        """Honor a stop request made while the worker was loading its model.

        Use the same event check as an optimizer boundary before training begins.
        The worker checks this flag to avoid optimization for an already stopped job.
        """
        return self.on_step_end(args, state, control, **kwargs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--run', required=True)
    options = parser.parse_args()
    execute(options.vault, options.run)
