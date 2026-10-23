import json
import sys
from unittest.mock import MagicMock

import pytest

from smpa_ros.backends.ollama import OllamaBackend
from smpa_ros.backends.transformers import TransformersBackend
from smpa_ros.loop import run_cycle


def test_cycle_order_and_independent_backends():
    calls = []

    class RecordingBackend:
        def __init__(self, name):
            self.name = name

        def generate(self, prompt, *, max_new_tokens):
            calls.append((self.name, prompt, max_new_tokens))
            return self.name + ' output'

    backends = {name: RecordingBackend(name) for name in ('sense', 'model', 'plan', 'act')}
    result = run_cycle('a red cup', **backends)
    assert list(result) == list(backends)
    previous = 'a red cup'
    for name, prompt, limit in calls:
        assert previous in prompt
        assert limit == (512 if name == 'model' else 128)
        previous = name + ' output'
        assert result[name] == previous


def test_failed_stage_stops_cycle():
    failed = MagicMock()
    failed.generate.side_effect = RuntimeError('inference failed')
    later = MagicMock()
    with pytest.raises(RuntimeError, match='inference failed'):
        run_cycle('observation', sense=failed, model=later, plan=later, act=later)
    later.generate.assert_not_called()


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
    from smpa_ros.loop import run

    repository = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repository / 'config/pipeline.yaml').read_text())
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
    from smpa_ros.loop import ACTION_SCHEMA
    torch = MagicMock()
    transformers = MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    library = ModuleType('letssee_llm.structured')
    library.logits_processor = MagicMock(return_value='token-mask')
    library.validate_response = MagicMock(return_value='{"action":"look_left"}')
    monkeypatch.setitem(sys.modules, 'letssee_llm', ModuleType('letssee_llm'))
    monkeypatch.setitem(sys.modules, 'letssee_llm.structured', library if available else None)
    tokenizer = transformers.AutoTokenizer.from_pretrained.return_value
    tokenizer.chat_template = 'test'
    tokenizer.decode.return_value = '{"action":"look_left"}'
    tokenizer.return_value.to.return_value = {'input_ids': MagicMock(shape=(1, 3))}
    backend = TransformersBackend('/local/model', enable_thinking=False)
    if available:
        assert backend.generate('choose', schema=ACTION_SCHEMA) == '{"action":"look_left"}'
        library.logits_processor.assert_called_once_with(backend.model, backend.tokenizer, ACTION_SCHEMA)
        assert backend.model.generate.call_args.kwargs['logits_processor'] == ['token-mask']
        library.validate_response.assert_called_once_with('{"action":"look_left"}', ACTION_SCHEMA)
    else:
        with pytest.raises(RuntimeError, match='no unrestricted fallback'):
            backend.generate('choose', schema=ACTION_SCHEMA)
        backend.model.generate.assert_not_called()
