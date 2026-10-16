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
        assert limit == 128
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
