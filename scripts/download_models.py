#!/usr/bin/env python3
"""Download the fixed SMPA library; rerun to resume incomplete attempts."""

import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import signal
import time

MODELS = [
    ('llm', 'qwen3_0.6b', 'Qwen/Qwen3-0.6B'),
    ('llm', 'qwen3_1.7b', 'Qwen/Qwen3-1.7B'),
    ('llm', 'qwen3_4b', 'Qwen/Qwen3-4B'),
    ('llm', 'qwen3_8b', 'Qwen/Qwen3-8B'),
    ('vlm', 'smolvlm2_500m', 'HuggingFaceTB/SmolVLM2-500M-Video-Instruct'),
    ('vlm', 'smolvlm2_2.2b', 'HuggingFaceTB/SmolVLM2-2.2B-Instruct'),
    ('vlm', 'qwen3_vl_2b', 'Qwen/Qwen3-VL-2B-Instruct'),
    ('vlm', 'qwen3_vl_4b', 'Qwen/Qwen3-VL-4B-Instruct'),
    ('vlm', 'qwen2.5_vl_3b', 'Qwen/Qwen2.5-VL-3B-Instruct'),
    ('coding', 'qwen2.5_coder_1.5b', 'Qwen/Qwen2.5-Coder-1.5B-Instruct'),
    ('coding', 'qwen2.5_coder_7b', 'Qwen/Qwen2.5-Coder-7B-Instruct'),
]
VISUAL_JEV_MODELS = [
    ('vision', 'visual-jev-4b/base_model', 'Qwen/Qwen3-VL-4B-Instruct'),
    ('vision', 'visual-jev-4b/adapter', 'guanxuyu/visual-jev-4b-answer-sft'),
]
ROOT = Path(__file__).resolve().parents[1] / 'models'
GIB = 1024 ** 3
RESERVE = 5 * GIB
CRITICAL = 2 * GIB


def disk_usage(directory):
    """Allocated local bytes, counting hard-linked files only once."""
    seen = set()
    total = 0
    for path in directory.rglob('*'):
        if path.is_file():
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            if key not in seen:
                total += stat.st_blocks * 512
                seen.add(key)
    return total


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified(directory, manifest):
    return bool(manifest) and all(
        (directory / name).is_file()
        and (directory / name).stat().st_size == details['size']
        and (not details.get('sha256') or digest(directory / name) == details['sha256'])
        for name, details in manifest.items()
    )


def save_report(path, records):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump({'models': list(records.values())}, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def download(model, root, results):
    """One bounded worker; Hub checks are deliberately anonymous."""
    category, folder, repo = model
    directory = root / category / folder
    result = {'status': 'failed', 'error': None, 'revision': None, 'files': {}}
    try:
        os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '20')
        os.environ.setdefault('HF_HUB_ETAG_TIMEOUT', '20')
        from huggingface_hub import HfApi, snapshot_download, try_to_load_from_cache
        from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError, HfHubHTTPError

        try:
            info = HfApi(token=False).model_info(repo, files_metadata=True, timeout=20)
        except (GatedRepoError, RepositoryNotFoundError) as error:
            result.update(status='skipped', error=f'{type(error).__name__}: {error}')
            return
        except HfHubHTTPError as error:
            if error.response is not None and error.response.status_code in (401, 403, 404):
                result.update(status='skipped', error=f'Anonymous access denied: {error}')
                return
            raise
        if info.gated or info.private:
            result.update(status='skipped', error='Repository is gated or private')
            return
        result['revision'] = info.sha
        results.put({'revision': info.sha})
        # Root HF inference files only: avoid alternate weight formats/subfolders.
        siblings = [file for file in info.siblings if '/' not in file.rfilename
                    and file.rfilename.endswith(('.json', '.safetensors', '.model',
                                                 '.txt', '.tiktoken', '.jinja'))]
        names = {file.rfilename for file in siblings}
        is_adapter = repo == 'guanxuyu/visual-jev-4b-answer-sft'
        config_name = 'adapter_config.json' if is_adapter else 'config.json'
        if config_name not in names or not any(name.endswith('.safetensors') for name in names):
            raise ValueError('Missing model config or safetensors weights')
        if not is_adapter and not any(name in names for name in ('tokenizer.json', 'tokenizer.model')):
            raise ValueError('Missing tokenizer')
        if (category == 'vlm' or (category == 'vision' and not is_adapter)) and not any(name in names for name in
                                       ('preprocessor_config.json', 'processor_config.json')):
            raise ValueError('Missing processor configuration')
        directory.mkdir(parents=True, exist_ok=True)
        needed = 0
        reused = set()
        report_path = root / 'download_report.json'
        previous = json.loads(report_path.read_text()).get('models', []) if report_path.exists() else []
        for file in siblings:
            if file.size is None:
                raise ValueError(f'Unknown file size: {file.rfilename}')
            target = directory / file.rfilename
            cached = try_to_load_from_cache(repo, file.rfilename, revision=info.sha)
            if not isinstance(cached, str):
                for item in previous:
                    candidate = Path(item['local_dir']) / file.rfilename
                    if item['repo_id'] == repo and item.get('revision') == info.sha and candidate.is_file():
                        cached = str(candidate)
                        break
            if not target.exists() and isinstance(cached, str):
                source = Path(cached).resolve()
                if source.stat().st_size == file.size:
                    try:
                        os.link(source, target)
                    except OSError:
                        target.symlink_to(source)
            if isinstance(cached, str) and target.exists() and os.path.samefile(target, cached):
                reused.add(file.rfilename)
            if not target.exists() or target.stat().st_size != file.size:
                needed += file.size
            result['files'][file.rfilename] = {
                'size': file.size,
                'sha256': file.lfs.sha256 if file.lfs else None,
            }
        if shutil.disk_usage(root).free < needed + RESERVE:
            result.update(status='skipped', error='Insufficient storage (5 GiB reserve required)')
            return
        # Hub's local_dir code otherwise attempts to copy cache files onto our links.
        if names - reused:
            snapshot_download(repo, revision=info.sha, local_dir=directory,
                              allow_patterns=sorted(names - reused), token=False,
                              max_workers=2, etag_timeout=20)
        if not verified(directory, result['files']):
            # Replace only corrupt files, leaving valid weights and the cache intact.
            bad = [name for name, details in result['files'].items()
                   if not verified(directory, {name: details})]
            snapshot_download(repo, revision=info.sha, local_dir=directory,
                              allow_patterns=bad, token=False, max_workers=2,
                              etag_timeout=20, force_download=True)
            if not verified(directory, result['files']):
                raise ValueError('Downloaded files failed size/checksum verification')
        if model == MODELS[0]:
            from transformers import AutoConfig, AutoTokenizer
            AutoConfig.from_pretrained(directory, local_files_only=True)
            AutoTokenizer.from_pretrained(directory, local_files_only=True)
        result['status'] = 'completed'
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'[:2000]
    finally:
        results.put(result)


def attempt(model, root, timeout):
    # A process deadline also bounds native transfer code and Hub internal retries.
    context = multiprocessing.get_context('spawn')
    results = context.Queue()
    process = context.Process(target=download, args=(model, root, results))
    process.start()
    deadline = time.monotonic() + timeout
    progress = {}
    try:
        while True:
            try:
                message = results.get(timeout=1)
            except queue.Empty:
                if not process.is_alive():
                    return {**progress, 'status': 'failed',
                            'error': f'Download worker exited with {process.exitcode}'}
            else:
                if 'status' in message:
                    return {**progress, **message}
                progress.update(message)
            if shutil.disk_usage(root).free < CRITICAL:
                return {**progress, 'status': 'skipped', 'error': 'Critically low disk space'}
            if time.monotonic() >= deadline:
                return {**progress, 'status': 'failed',
                        'error': f'Attempt timed out after {timeout:g}s'}
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()
        results.close()


def record_attempt(model, result, root, records, report):
    category, folder, repo = model
    directory = root / category / folder
    record = {
        'name': repo.split('/')[-1], 'repo_id': repo, 'category': category,
        'local_dir': str(directory.resolve()), 'revision': result.get('revision'),
        'files': result.get('files', {}), 'status': result['status'],
        'error': result.get('error'), 'local_disk_usage_bytes': disk_usage(directory),
    }
    records[repo] = record
    save_report(report, records)
    return record


def run(root=ROOT, retries=2, timeout=1800, models=None):
    models = MODELS if models is None else models
    root.mkdir(parents=True, exist_ok=True)
    report = root / 'download_report.json'
    records = {}
    if report.exists():
        try:
            records = {item['repo_id']: item for item in json.loads(report.read_text())['models']}
        except (ValueError, KeyError, TypeError):
            print('Previous report is unreadable; checking models again.', flush=True)
    print(f'SMPA ROS MODEL DOWNLOADER\nModels scheduled: {len(models)}', flush=True)
    for index, model in enumerate(models, 1):
        category, folder, repo = model
        directory = root / category / folder
        print(f'\n[{index}/{len(models)}] {repo}\nCategory: {category}\n'
              f'Destination: {directory}', flush=True)
        previous = records.get(repo, {})
        interrupted = False
        result = {'revision': None, 'files': {}}
        try:
            if previous.get('status') == 'completed' and verified(directory, previous.get('files')):
                result = previous
                print('Previously completed files verified; reusing.', flush=True)
            else:
                for retry in range(retries + 1):
                    print(f'Status: Downloading (attempt {retry + 1}/{retries + 1})', flush=True)
                    result = attempt(model, root, timeout)
                    record_attempt(model, result, root, records, report)
                    if result['status'] != 'failed':
                        break
                    print(result['error'], flush=True)
                    if retry < retries:
                        time.sleep(2)
        except KeyboardInterrupt:
            result.update(status='failed', error='Interrupted; partial files preserved')
            interrupted = True
        except Exception as error:
            result.update(status='failed', error=f'{type(error).__name__}: {error}')
        result = record_attempt(model, result, root, records, report)
        print(f"[{index}/{len(models)}] {result['status'].upper()}"
              f" — {result['error'] or directory}", flush=True)
        completed = sum(records.get(item[2], {}).get('status') == 'completed' for item in models)
        print(f'Completed: {completed}/{len(models)}; remaining to attempt: '
              f'{len(models) - index}', flush=True)
        if interrupted or shutil.disk_usage(root).free < CRITICAL:
            print('Stopping safely; rerun to resume.', flush=True)
            break
    print(f'\nSMPA ROS MODEL LIBRARY\nTotal models: {len(models)}', flush=True)
    for status in ('completed', 'skipped', 'failed'):
        items = [item for repo, item in records.items()
                 if repo in {model[2] for model in models} and item['status'] == status]
        print(f'{status.upper()}: {len(items)}/{len(models)}', flush=True)
        for item in items:
            print(f"  {item['name']}: {item['error'] or item['revision']}", flush=True)
    print(f'Library location: {root.resolve()}', flush=True)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retries', type=int, default=2, help='Retries per failed model')
    parser.add_argument('--timeout', type=float, default=1800, help='Seconds per attempt')
    parser.add_argument('--visual-jev', action='store_true', help='Download only the Visual Jev base and adapter')
    options = parser.parse_args()
    if options.retries < 0 or options.timeout <= 0:
        parser.error('retries must be nonnegative and timeout must be positive')
    # Also save the current attempt when a terminal sends SIGTERM.
    def interrupt(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    run(retries=options.retries, timeout=options.timeout,
        models=VISUAL_JEV_MODELS if options.visual_jev else None)


if __name__ == '__main__':
    main()
