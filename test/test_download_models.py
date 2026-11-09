import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from queue import Queue
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def downloader():
    path = Path(__file__).resolve().parents[1] / 'scripts/download_models.py'
    spec = importlib.util.spec_from_file_location('download_models', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(downloader, root, model):
    category, folder, _ = model
    directory = root / category / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'model.safetensors'
    path.write_bytes(b'test weights')
    return {'status': 'completed', 'error': None, 'revision': 'test-commit',
            'files': {path.name: {'size': path.stat().st_size,
                                  'sha256': downloader.digest(path)}}}


def test_success_and_completed_restart(downloader, tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, 'MODELS', downloader.MODELS[:1])
    calls = []

    def attempt(model, root, timeout):
        calls.append(model)
        return completed(downloader, root, model)

    monkeypatch.setattr(downloader, 'attempt', attempt)
    downloader.run(tmp_path, retries=0)
    records = downloader.run(tmp_path, retries=0)
    assert len(calls) == 1
    assert records[calls[0][2]]['status'] == 'completed'
    report = json.loads((tmp_path / 'download_report.json').read_text())
    assert report['models'][0]['revision'] == 'test-commit'
    # A same-sized corrupt weight must not be accepted on restart.
    weight = tmp_path / calls[0][0] / calls[0][1] / 'model.safetensors'
    weight.write_bytes(b'bad! weights')
    downloader.run(tmp_path, retries=0)
    assert len(calls) == 2


def test_failure_gated_and_continuation(downloader, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(downloader, 'MODELS', downloader.MODELS[:3])
    calls = []
    saved = []
    save_report = downloader.save_report

    def save(path, records):
        saved.append([record['status'] for record in records.values()])
        save_report(path, records)

    monkeypatch.setattr(downloader, 'save_report', save)

    def attempt(model, root, timeout):
        calls.append(model)
        if model == downloader.MODELS[0]:
            return {'status': 'failed', 'error': 'Network timeout'}
        if model == downloader.MODELS[1]:
            return {'status': 'skipped', 'error': 'Repository is gated'}
        return completed(downloader, root, model)

    monkeypatch.setattr(downloader, 'attempt', attempt)
    monkeypatch.setattr(downloader.time, 'sleep', lambda _: None)
    records = downloader.run(tmp_path, retries=1)
    assert len(calls) == 4
    assert saved[:2] == [['failed'], ['failed']]
    assert [record['status'] for record in records.values()] == ['failed', 'skipped', 'completed']
    assert len(json.loads((tmp_path / 'download_report.json').read_text())['models']) == 3
    assert 'COMPLETED: 1/3' in capsys.readouterr().out


def test_interruption_and_restart(downloader, tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, 'MODELS', downloader.MODELS[:1])

    def interrupt(model, root, timeout):
        directory = root / model[0] / model[1]
        directory.mkdir(parents=True)
        (directory / 'weights.incomplete').write_bytes(b'partial')
        raise KeyboardInterrupt

    monkeypatch.setattr(downloader, 'attempt', interrupt)
    records = downloader.run(tmp_path, retries=0)
    assert next(iter(records.values()))['status'] == 'failed'
    monkeypatch.setattr(downloader, 'attempt',
                        lambda model, root, timeout: completed(downloader, root, model))
    records = downloader.run(tmp_path, retries=0)
    assert next(iter(records.values()))['status'] == 'completed'
    assert list(tmp_path.rglob('*.incomplete'))
    assert not list(tmp_path.glob('*.tmp'))


def test_git_protection():
    root = Path(__file__).resolve().parents[1]
    artifacts = ['models/text/qwen3-4b/tokenizer.json', 'models/llm/qwen3_0.6b/model.safetensors',
                 'models/vlm/smolvlm2_2.2b/tokenizer.json',
                 'models/coding/qwen2.5_coder_7b/.cache/huggingface/file.incomplete',
                 'models/download_report.json', 'models/download_report.json.tmp']
    for name in artifacts:
        assert subprocess.run(['git', 'check-ignore', '-q', name], cwd=root).returncode == 0
    assert subprocess.run(['git', 'check-ignore', '-q', 'models/smolvlm2.yaml'],
                          cwd=root).returncode == 1


def test_anonymous_gated_repository_is_not_downloaded(downloader, tmp_path, monkeypatch):
    calls = []

    class AccessError(Exception):
        pass

    def api(*, token):
        assert token is False
        return SimpleNamespace(model_info=lambda *args, **kwargs:
                               SimpleNamespace(gated=True, private=False))

    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        HfApi=api, snapshot_download=lambda *args, **kwargs: calls.append(args),
        try_to_load_from_cache=lambda *args, **kwargs: None,
    ))
    monkeypatch.setitem(sys.modules, 'huggingface_hub.errors', SimpleNamespace(
        GatedRepoError=AccessError, RepositoryNotFoundError=AccessError,
        HfHubHTTPError=AccessError,
    ))
    results = Queue()
    downloader.download(downloader.MODELS[0], tmp_path, results)
    assert results.get()['status'] == 'skipped'
    assert not calls


def test_cache_links_are_not_copied_onto_themselves(downloader, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    cache.mkdir()
    contents = {'config.json': b'{}', 'tokenizer.json': b'{}',
                'model.safetensors': b'cached weights', 'preprocessor_config.json': b'{}'}
    for name, data in contents.items():
        (cache / name).write_bytes(data)
    calls = []
    info = SimpleNamespace(gated=False, private=False, sha='cached-revision', siblings=[
        SimpleNamespace(rfilename=name, size=len(data), lfs=None)
        for name, data in contents.items()
    ])
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        HfApi=lambda **kwargs: SimpleNamespace(model_info=lambda *args, **kwargs: info),
        snapshot_download=lambda *args, **kwargs: calls.append(kwargs),
        try_to_load_from_cache=lambda repo, name, **kwargs: str(cache / name),
    ))

    class AccessError(Exception):
        pass

    monkeypatch.setitem(sys.modules, 'huggingface_hub.errors', SimpleNamespace(
        GatedRepoError=AccessError, RepositoryNotFoundError=AccessError,
        HfHubHTTPError=AccessError,
    ))
    results = Queue()
    model = downloader.MODELS[5]
    downloader.download(model, tmp_path, results)
    assert results.get() == {'revision': 'cached-revision'}
    assert results.get()['status'] == 'completed'
    assert not calls
    target = tmp_path / model[0] / model[1] / 'model.safetensors'
    assert target.samefile(cache / 'model.safetensors')


def test_timeout_preserves_known_revision(downloader, tmp_path, monkeypatch):
    results = MagicMock()
    results.get.return_value = {'revision': 'known-commit'}
    process = MagicMock()
    context = SimpleNamespace(Queue=lambda: results, Process=lambda **kwargs: process)
    monkeypatch.setattr(downloader.multiprocessing, 'get_context', lambda _: context)
    result = downloader.attempt(downloader.MODELS[0], tmp_path, timeout=0)
    assert result['status'] == 'failed'
    assert result['revision'] == 'known-commit'
    process.terminate.assert_called_once()


def test_registered_paths_and_legacy_models(downloader):
    for name in ('qwen3-1.7b', 'qwen3-4b', 'phi-4-mini-instruct', 'gemma-4-e4b', 'qwen3-8b'):
        model = downloader.REGISTERED[name]
        assert model[:2] == ('text', name)
        assert len(downloader.REVISIONS[model[2]]) == 40
        assert model in downloader.MODELS
    assert downloader.REGISTERED['qwen3-0.6b'][:2] == ('llm', 'qwen3_0.6b')
    assert downloader.ROOT == Path(__file__).resolve().parents[1] / 'models'


@pytest.mark.parametrize('selection', ['qwen3-4b', 'Qwen/Qwen3-4B'])
def test_cli_selection_and_storage_override(downloader, tmp_path, monkeypatch, selection):
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return {'Qwen/Qwen3-4B': {'status': 'completed'}}
    monkeypatch.setattr(downloader, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['download_models.py', '--model', selection,
                                    '--model-root', str(tmp_path)])
    downloader.main()
    assert calls[0]['models'] == [('text', 'qwen3-4b', 'Qwen/Qwen3-4B')]
    assert calls[0]['root'] == tmp_path


def test_cli_all_text_and_list(downloader, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(downloader, 'run', lambda **kwargs: calls.append(kwargs) or {
        model[2]: {'status': 'completed'} for model in kwargs['models']})
    monkeypatch.setattr(sys, 'argv', ['download_models.py', '--all-text'])
    downloader.main()
    assert calls[0]['models'] == list(downloader.REGISTERED.values())
    monkeypatch.setattr(sys, 'argv', ['download_models.py', '--list', '--model-root', str(tmp_path)])
    downloader.main()
    assert len(calls) == 1
    output = capsys.readouterr().out
    assert 'qwen3-4b | Qwen/Qwen3-4B | unavailable' in output
    assert str(tmp_path / 'text/qwen3-4b') in output


def test_changed_revision_does_not_reuse_completed_report(downloader, tmp_path, monkeypatch):
    model = downloader.REGISTERED['qwen3-4b']
    calls = []
    def attempt(model, root, timeout):
        calls.append(model)
        return completed(downloader, root, model)
    monkeypatch.setattr(downloader, 'attempt', attempt)
    downloader.run(tmp_path, retries=0, models=[model])
    downloader.run(tmp_path, retries=0, models=[model])
    assert len(calls) == 2  # Test report revision differs from the registry pin.


def test_identical_weights_reused_from_other_cached_revision(downloader, tmp_path, monkeypatch):
    import hashlib
    contents = {'config.json': b'{}', 'tokenizer.json': b'{}',
                'model.safetensors': b'identical weights', 'preprocessor_config.json': b'{}'}
    cached = tmp_path / 'cached-weights'
    cached.write_bytes(contents['model.safetensors'])
    calls = []
    info = SimpleNamespace(gated=False, private=False, sha='new-revision', siblings=[
        SimpleNamespace(rfilename=name, size=len(data),
                        lfs=SimpleNamespace(sha256=hashlib.sha256(data).hexdigest())
                        if name.endswith('.safetensors') else None)
        for name, data in contents.items()])
    def snapshot(repo, **kwargs):
        calls.append(kwargs['allow_patterns'])
        for name in kwargs['allow_patterns']:
            (kwargs['local_dir'] / name).write_bytes(contents[name])
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        HfApi=lambda **kwargs: SimpleNamespace(model_info=lambda *args, **kwargs: info),
        snapshot_download=snapshot,
        try_to_load_from_cache=lambda repo, name, **kwargs:
            str(cached) if name == 'model.safetensors' and not kwargs else None))
    monkeypatch.setitem(sys.modules, 'huggingface_hub.errors', SimpleNamespace(
        GatedRepoError=RuntimeError, RepositoryNotFoundError=RuntimeError, HfHubHTTPError=RuntimeError))
    results = Queue()
    model = downloader.MODELS[5]
    downloader.download(model, tmp_path, results)
    assert results.get()['revision'] == 'new-revision'
    assert results.get()['status'] == 'completed'
    assert 'model.safetensors' not in calls[0]
    assert (tmp_path / model[0] / model[1] / 'model.safetensors').samefile(cached)
