import json
import sys
from unittest.mock import MagicMock

import pytest

from smpa_ros.backends.ollama import OllamaBackend
from smpa_ros.backends.transformers import TransformersBackend
from runner import Runner


def test_cycle_order_and_independent_backends():
    from smpa_ros.smpa import run
    runner = MagicMock()
    run(runner, 2)
    assert [call[0] for call in runner.method_calls] == [
        'start', 'cycle', 'sense', 'model', 'plan', 'act',
        'cycle', 'sense', 'model', 'plan', 'act', 'finish']
    runner.model.assert_called_with(runner.sense.return_value, prompt='default')
    runner.plan.assert_called_with(runner.model.return_value, prompt='default')
    runner.act.assert_called_with(runner.plan.return_value, prompt='default')


def test_failed_stage_stops_cycle():
    from smpa_ros.smpa import run
    runner = MagicMock()
    runner.sense.side_effect = RuntimeError('inference failed')
    with pytest.raises(RuntimeError, match='inference failed'):
        run(runner, 1)
    runner.model.assert_not_called()
    runner.plan.assert_not_called()
    runner.act.assert_not_called()


def test_ollama_request_without_server(monkeypatch):
    requests = []

    def respond(request, timeout):
        requests.append((request, timeout))
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"response": "facts"}'
        return response

    monkeypatch.setattr('smpa_ros.backends.ollama.urlopen', respond)
    backend = OllamaBackend('local-model', timeout=5)
    assert backend.generate('observation', max_new_tokens=12) == 'facts'
    request, timeout = requests[0]
    assert request.full_url == 'http://localhost:11434/api/generate'
    assert timeout == 5
    assert json.loads(request.data) == {
        'model': 'local-model', 'prompt': 'observation', 'stream': False,
        'options': {'temperature': 0, 'seed': 0, 'num_predict': 12},
    }


@pytest.mark.parametrize('chat_template', [None, 'test template'])
def test_transformers_local_loading_and_continuation(monkeypatch, chat_template):
    torch = MagicMock()
    transformers = MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    tokenizer = transformers.AutoTokenizer.from_pretrained.return_value
    tokenizer.chat_template = chat_template
    tokenizer.apply_chat_template.return_value = 'formatted prompt'
    input_ids = MagicMock()
    input_ids.shape = (1, 3)
    tokenizer.return_value.to.return_value = {'input_ids': input_ids}
    tokenizer.decode.return_value = 'continuation'
    model = transformers.AutoModelForCausalLM.from_pretrained.return_value

    backend = TransformersBackend('/local/checkpoint')
    assert backend.generate('prompt', max_new_tokens=7) == 'continuation'
    transformers.AutoTokenizer.from_pretrained.assert_called_once_with(
        '/local/checkpoint', local_files_only=True,
    )
    transformers.AutoModelForCausalLM.from_pretrained.assert_called_once_with(
        '/local/checkpoint', local_files_only=True,
    )
    model.eval.assert_called_once_with()
    model.generate.assert_called_once_with(
        input_ids=input_ids, max_new_tokens=7, do_sample=False,
    )
    model.generate.return_value.__getitem__.assert_called_once_with((0, slice(3, None)))
    torch.no_grad.assert_called_once_with()
    if chat_template:
        tokenizer.assert_called_once_with(
            'formatted prompt', return_tensors='pt', add_special_tokens=False,
        )
    else:
        tokenizer.assert_called_once_with('prompt', return_tensors='pt')


@pytest.mark.integration
def test_local_llm_world_model_iterations(tmp_path):
    """Optional genuine CPU inference using the already provisioned modeling checkpoint."""
    import importlib.util
    from pathlib import Path
    import yaml

    repository = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repository / 'config.yaml').read_text())
    if config['modeling'].get('backend') == 'api':
        pytest.skip('API modeling is configured; no local modeling checkpoint selected')
    registry = yaml.safe_load((repository / 'models/model_library.yaml').read_text())['models']
    selected = registry[config['modeling']['model']]
    checkpoint = repository / 'models' / selected.get('path', '')
    if (selected['backend'] != 'transformers' or not (checkpoint / 'model.safetensors').is_file()
            or importlib.util.find_spec('torch') is None or importlib.util.find_spec('transformers') is None):
        pytest.skip('Optional local Transformers modeling checkpoint/dependencies are unavailable; no download attempted')
    import torch
    previous_threads = torch.get_num_threads()
    torch.set_num_threads(2)
    try:
        backend = TransformersBackend(str(checkpoint), enable_thinking=selected.get('enable_thinking'))
        assert isinstance(backend.model, torch.nn.Module)
        observations = ['A white cup is on a wooden table.',
                        'The same white cup is still on the table. A red book is now next to it.']
        from test.test_run_storage import run
        directory = run(2, root=tmp_path, sense=lambda directory, cycle: observations[cycle - 1],
                        model=backend, model_identifier=selected['model_id'],
                        plan=lambda prompt, **settings: '{"action":"look_left"}',
                        act=lambda action, record: record.update(goal_accepted=True, result_status=4, success=True))
        snapshots = [(directory / f'world_model/iteration_{cycle:03d}.md').read_text() for cycle in (1, 2)]
        for snapshot in snapshots:
            assert '# World Model' in snapshot and '## Entities' in snapshot
            assert 'cup' in snapshot.lower()
            assert snapshot not in observations
        second = json.loads((directory / 'state/cycle_000002/model.json').read_text())
        assert snapshots[0] in second['prompt']
        assert second['output'] == snapshots[1]
        assert second['model_identifier'] == selected['model_id']
        assert json.loads((directory / 'state/cycle_000002/plan.json').read_text())['input'] == snapshots[1]
        assert yaml.safe_load((directory / 'metadata.yaml').read_text())['completed_cycles'] == 2
        print(f'Genuine local LLM inference: {selected["model_id"]}, two persisted snapshots')
    finally:
        torch.set_num_threads(previous_threads)


@pytest.mark.parametrize('available', [True, False])
def test_transformers_schema_constraints_never_fall_back(monkeypatch, available):
    from types import ModuleType
    from smpa_ros.plan import ACTION_SCHEMA
    torch = MagicMock()
    transformers = MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    library = ModuleType('outlines')
    library.from_transformers = MagicMock()
    processors = ModuleType('outlines.backends')
    processors.get_json_schema_logits_processor = MagicMock(return_value='token-mask')
    monkeypatch.setitem(sys.modules, 'outlines', library if available else None)
    monkeypatch.setitem(sys.modules, 'outlines.backends', processors if available else None)
    tokenizer = transformers.AutoTokenizer.from_pretrained.return_value
    tokenizer.chat_template = 'test'
    tokenizer.decode.return_value = '{"action":"look_left"}'
    tokenizer.return_value.to.return_value = {'input_ids': MagicMock(shape=(1, 3))}
    backend = TransformersBackend('/local/model', enable_thinking=False)
    if available:
        assert backend.generate('choose', schema=ACTION_SCHEMA) == '{"action":"look_left"}'
        library.from_transformers.assert_called_once_with(backend.model, backend.tokenizer)
        processors.get_json_schema_logits_processor.assert_called_once_with(
            'outlines_core', library.from_transformers.return_value, json.dumps(ACTION_SCHEMA))
        assert backend.model.generate.call_args.kwargs['logits_processor'] == ['token-mask']
    else:
        with pytest.raises(RuntimeError, match='[Nn]o unrestricted fallback'):
            backend.generate('choose', schema=ACTION_SCHEMA)
        backend.model.generate.assert_not_called()


@pytest.mark.parametrize('backend_name', ['transformers', 'openai', 'nebula', 'ollama'])
def test_shared_text_backend_selection(tmp_path, monkeypatch, backend_name):
    from pathlib import Path
    import yaml
    from smpa_ros.model import load_backend
    from smpa_ros.plan import ACTION_SCHEMA

    repository = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repository / 'config.yaml').read_text())
    vision = dict(config['vision'])
    registry = yaml.safe_load((repository / 'models/model_library.yaml').read_text())
    registry['models']['test-ollama'] = {'backend': 'ollama', 'model_id': 'test-model'}
    (tmp_path / 'model_library.yaml').write_text(yaml.safe_dump(registry))
    if backend_name in ('openai', 'nebula'):
        config['modeling'] = {'backend': 'api', 'provider': backend_name}
        target = 'smpa_ros.backends.api.APIBackend'
    elif backend_name == 'transformers':
        config['modeling'] = {'model': 'qwen3-0.6b'}
        target = 'smpa_ros.backends.transformers.TransformersBackend'
    else:
        config['modeling'] = {'model': 'test-ollama'}
        target = 'smpa_ros.backends.ollama.OllamaBackend'
    constructor = MagicMock()
    constructor.return_value.generate.return_value = 'generated text'
    monkeypatch.setattr(target, constructor)
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    generate, metadata = load_backend(config, config_path, tmp_path / 'weights', tmp_path)
    constructor.assert_not_called()
    assert config['vision'] == vision
    assert generate('world', max_new_tokens=37) == 'generated text'
    if backend_name in ('openai', 'nebula'):
        constructor.assert_called_once_with(config_path, modality='text', provider=backend_name)
        assert metadata['model_id'] == config[backend_name]['models']['text']
    elif backend_name == 'transformers':
        constructor.assert_called_once_with(str(tmp_path / 'weights/llm/qwen3_0.6b'), enable_thinking=False)
        assert metadata['model_id'] == 'Qwen/Qwen3-0.6B'
    else:
        constructor.assert_called_once_with('test-model')
        assert metadata['model_id'] == 'test-model'
    if backend_name == 'ollama':
        with pytest.raises(RuntimeError, match='schema-constrained planning'):
            generate('choose', max_new_tokens=11, schema=ACTION_SCHEMA)
        assert constructor.return_value.generate.call_count == 1
    else:
        options = {'reasoning_effort': 'low'} if backend_name in ('openai', 'nebula') else {}
        generate('choose', max_new_tokens=11, schema=ACTION_SCHEMA, **options)
        constructor.return_value.generate.assert_called_with(
            'choose', max_new_tokens=11, schema=ACTION_SCHEMA, **options)
    assert constructor.call_count == 1


@pytest.mark.parametrize('stage, expected', [('sense', 128), ('model', 512), ('plan', 128), ('act', 128)])
def test_unified_stage_generation_defaults(stage, expected):
    import importlib
    from pathlib import Path
    import yaml
    module = importlib.import_module('smpa_ros.' + stage)
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config.yaml').read_text())
    assert module.load_prompt('input')[1] == {'max_new_tokens': expected}
    assert module.load_prompt('input', configuration=config)[1] == {'max_new_tokens': expected}
    config['stage_settings'][stage]['max_new_tokens'] = 37
    assert module.load_prompt('input', configuration=config)[1] == {'max_new_tokens': 37}
