"""Exercise installer configuration and provisioning without network or weights."""
import ast
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
import yaml

from scripts import download_models

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / 'install.sh').read_text()


def installer_block(name):
    """Read one named Python program embedded in the installer.

    The delimiter identifies the actual production code, so tests execute the
    installer's decisions rather than a second implementation of those decisions.
    Return compiled code and fail immediately if the delimiter is missing.
    """
    source = INSTALLER.split(f"<<'{name}'\n", 1)[1].split(f'\n{name}\n', 1)[0]
    return compile(source, f'install.sh:{name}', 'exec')


def write_model(directory):
    """Create the smallest local snapshot accepted by the runtime file checks.

    Write a supported vision configuration, tokenizer, processor and nonempty
    weight file into the temporary directory. These files test provisioning
    decisions only; they are deliberately not loadable inference weights.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.json').write_text(json.dumps({'model_type': 'qwen3_vl'}))
    (directory / 'tokenizer.json').write_text('{}')
    (directory / 'preprocessor_config.json').write_text('{}')
    (directory / 'model.safetensors').write_bytes(b'test snapshot')


@pytest.fixture
def provision(tmp_path, monkeypatch):
    """Run the real provisioning program against a temporary checkout.

    Copy tracked model definitions and replace only network downloading with
    snapshot creation. Return the executor and download mock so tests can inspect
    selection, failures and local-path handling without changing real models.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'models').mkdir()
    for source in (ROOT / 'models').glob('*.yaml'):
        (tmp_path / 'models' / source.name).write_text(source.read_text())

    def download(root, models):
        """Simulate completed downloads at the downloader's real directory layout.

        Create snapshots and a report for the requested tuples. Return the same
        completed records that production provisioning checks for failures.
        """
        records = {}
        for category, folder, model_id in models:
            directory = root / category / folder
            write_model(directory)
            records[model_id] = dict(repo_id=model_id, status='completed', local_dir=str(directory))
        (root / 'download_report.json').write_text(json.dumps({'models': list(records.values())}))
        return records

    downloads = Mock(side_effect=download)
    monkeypatch.setattr(download_models, 'run', downloads)

    def execute(config, *arguments):
        """Write the supplied pipeline and execute the installer's provisioning.

        Set the argument list exactly as the shell does. Runtime path validation
        remains real, and configuration or provisioning errors propagate to tests.
        """
        (tmp_path / 'config.yaml').write_text(yaml.safe_dump(config))
        monkeypatch.setattr(sys, 'argv', ['-', *arguments])
        exec(installer_block('PYMODELS'), {})

    return execute, downloads


def test_shared_model_provisions_only_selected_snapshot(provision):
    """Provision a fresh relative shared selection without any preexisting files.

    Check that only its model tuple is downloaded and that the runtime accepts
    the resulting local path for the shared cognitive stages.
    """
    execute, downloads = provision
    execute({'model': {'backend': 'transformers', 'model_id': 'Qwen/Qwen3-VL-2B-Instruct',
                       'path': 'models/vlm/qwen3_vl_2b'}})
    assert downloads.call_args.kwargs['models'] == [download_models.MODELS[6]]


def test_legacy_selection_uses_sense_model(provision):
    """Preserve runtime normalization of a legacy model-name configuration.

    Provision the Sense selection and ignore the historical modeling selection,
    matching the existing runtime's migration to one shared cognitive model.
    """
    execute, downloads = provision
    execute({'model': 'smolvlm2', 'vision': {'backend': 'transformers'},
             'modeling': {'model': 'qwen3-0.6b'}})
    assert downloads.call_args.kwargs['models'] == [download_models.MODELS[5]]


def test_complete_explicit_model_is_preserved(provision, tmp_path):
    """Accept an existing custom snapshot without replacing or downloading it.

    Use an unregistered identifier with an explicit directory and check that
    real local-file validation succeeds without scheduling network work.
    """
    execute, downloads = provision
    directory = tmp_path / 'custom'
    write_model(directory)
    execute({'model': {'path': str(directory), 'model_id': 'custom/model'}})
    downloads.assert_not_called()


def test_missing_explicit_model_fails_before_download(provision, tmp_path):
    """Reject an unavailable explicit path instead of downloading elsewhere.

    A known Hub identifier must not override the configured destination. Check
    the actionable error and confirm that no unrelated snapshot was downloaded.
    """
    execute, downloads = provision
    with pytest.raises(SystemExit, match='Configured model path.*Provision that explicit path'):
        execute({'model': {'path': str(tmp_path / 'missing'),
                           'model_id': 'Qwen/Qwen3-VL-2B-Instruct'}})
    downloads.assert_not_called()


def test_api_selection_does_not_require_local_weights(provision):
    """Permit a shared API selection without downloading or contacting its API.

    Supply its explicit model identifier and the existing provider defaults.
    Verify that installation schedules no local model downloads.
    """
    execute, downloads = provision
    execute({'model': {'backend': 'api', 'model_id': 'remote/model'},
             'openai': {'models': {'vision': 'remote/default'}}})
    downloads.assert_not_called()


def test_all_models_is_explicit_opt_in(provision):
    """Keep the existing all-models option independent of an API selection.

    Request the option explicitly and check that every existing downloader tuple
    is retained, with no additions or changes to the model library.
    """
    execute, downloads = provision
    execute({'model': {'backend': 'api', 'model_id': 'remote/model'},
             'openai': {'models': {'vision': 'remote/default'}}}, '--all-models')
    assert downloads.call_args.kwargs['models'] == download_models.MODELS


def test_incomplete_download_fails_installation(provision):
    """Make a failed selected download stop installation before the ROS build.

    Return a failed record from the download boundary and require the installer
    to identify the incomplete model rather than announcing successful setup.
    """
    execute, downloads = provision
    downloads.side_effect = None
    downloads.return_value = {'Qwen/Qwen3-VL-2B-Instruct': {'status': 'failed'}}
    with pytest.raises(SystemExit, match='Requested models are incomplete'):
        execute({'model': {'model_id': 'Qwen/Qwen3-VL-2B-Instruct',
                           'path': 'vlm/qwen3_vl_2b'}})


def test_dependency_declarations_and_shell_programs(tmp_path, monkeypatch):
    """Check declaration extraction, moved speech requirements and shell syntax.

    Generate constraints from the real setup declaration in a temporary checkout.
    Compare their values, compile all embedded Python programs, and ask Bash to
    validate its syntax. No installation or repository files are changed.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.venv').mkdir()
    declared = []
    for package in ('smpa_ros', 'smpa_llm'):
        (tmp_path / package).mkdir()
        (tmp_path / package / 'setup.py').write_text((ROOT / package / 'setup.py').read_text())
        setup = ast.parse((tmp_path / package / 'setup.py').read_text())
        call = next(node.value for node in setup.body if isinstance(node, ast.Expr) and
                    isinstance(node.value, ast.Call) and getattr(node.value.func, 'id', None) == 'setup')
        requirements = ast.literal_eval(next(item.value for item in call.keywords if item.arg == 'install_requires'))
        declared.extend(value for value in requirements if value not in declared)
    exec(installer_block('PYDEPS'), {})
    assert (tmp_path / '.venv/smpa-constraints.txt').read_text().splitlines() == declared
    assert '-r speech/requirements-speech.txt' in INSTALLER
    assert "'speech/requirements-speech.txt')" in INSTALLER
    assert (ROOT / 'speech/requirements-speech.txt').is_file()
    for name in ('PYVERIFY', 'PYMODELS', 'PYREADY'):
        installer_block(name)
    subprocess.run(['bash', '-n', str(ROOT / 'install.sh')], check=True)


def test_tracked_default_configuration_is_portable(provision):
    """Provision the tracked defaults inside an otherwise empty checkout.

    Require a relative path and execute the real default configuration against
    temporary snapshots. Check that its unchanged Qwen selection is downloaded
    once, without relying on any model in the developer's checkout.
    """
    execute, downloads = provision
    config = yaml.safe_load((ROOT / 'config.yaml').read_text())
    assert not Path(config['model']['path']).is_absolute()
    execute(config)
    assert downloads.call_args.kwargs['models'] == [download_models.MODELS[6]]
