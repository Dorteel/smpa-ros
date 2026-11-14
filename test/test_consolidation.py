"""Verify robot and conversational contracts at their shared integration points."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from rdflib.namespace import DCTERMS, RDF
import yaml

from memory import MemoryManager
from smpa_ros.backends.transformers import TransformersBackend
from smpa_ros.belief import read_graph
from smpa_ros.conversation import exchange, open_conversation, read_messages
from smpa_ros.conversation_web import discover_agents
from smpa_ros.model import unify_model
from smpa_ros.plan import ACTION_SCHEMA


ROOT = Path(__file__).resolve().parents[1]


def test_robot_generation_after_conversational_controls(monkeypatch):
    """Use one backend for conversation followed by constrained robot planning.

    Enable conversational sampling and thinking, then request canonical robot
    JSON with its own temperature and seed. Check that the schema mask is rebuilt,
    the conversational controls do not leak, and malformed robot JSON is rejected.
    Keep supplied multi-turn roles intact and leave the caller's history untouched.
    """
    backend = TransformersBackend.__new__(TransformersBackend)
    backend.enable_thinking = False
    backend.torch = MagicMock()
    backend.model = MagicMock()
    backend.model.config.model_type = 'qwen3'
    backend.model.generation_config.to_dict.return_value = {}
    backend.tokenizer = MagicMock()
    backend.tokenizer.chat_template = 'chat template'
    backend.tokenizer.return_value.to.return_value['input_ids'].shape = (1, 5)
    backend.tokenizer.decode.return_value = '<think>Explicit trace</think>Reply'
    options = Mock(side_effect=[{}, {'do_sample': False, 'logits_processor': ['mask']},
                                {'do_sample': False, 'logits_processor': ['fresh-mask']}])
    monkeypatch.setattr('smpa_ros.backends.transformers.generation_options', options)
    history = [{'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Hi'},
               {'role': 'user', 'content': 'Continue'}]
    before = deepcopy(history)
    result = backend.generate_conversation(history, do_sample=True, temperature=0.7,
        top_p=0.9, top_k=20, enable_thinking=True, seed=42)
    assert result['thinking'] == 'Explicit trace' and result['response'] == 'Reply'
    assert backend.tokenizer.apply_chat_template.call_args.args[0] == history == before
    backend.tokenizer.decode.return_value = '{"action":"look_left"}'
    output = backend.generate('Choose a robot action', schema=ACTION_SCHEMA,
        response_format='json_schema', max_new_tokens=64, temperature=0, seed=7)
    assert isinstance(output, str) and json.loads(output) == {'action': 'look_left'}
    options.assert_called_with(backend.model, backend.tokenizer, 0, ACTION_SCHEMA)
    generated = backend.model.generate.call_args.kwargs
    assert generated['do_sample'] is False and generated['logits_processor'] == ['mask']
    assert not {'top_p', 'top_k', 'repetition_penalty'} & generated.keys()
    assert backend.tokenizer.apply_chat_template.call_args.kwargs['enable_thinking'] is False
    backend.tokenizer.decode.return_value = '{"action":"unregistered_action"}'
    with pytest.raises(RuntimeError, match='invalid JSON|violating the selected schema'):
        backend.generate('Choose', schema=ACTION_SCHEMA, temperature=0)


def test_canonical_configuration_preserves_independent_selections(monkeypatch):
    """Discover conversational agents from the canonical robot configuration.

    Translate the canonical budget for agent provenance and omit null optional
    settings. Keep the robot's shared VLM, prompts and Plan schema unchanged while
    selecting the existing Qwen text default independently and loading no backend.
    """
    config = yaml.safe_load((ROOT / 'config.yaml').read_text())
    before = deepcopy(config)
    constructor = Mock(side_effect=AssertionError('Discovery must remain lazy'))
    monkeypatch.setattr('smpa_ros.backends.transformers.TransformersBackend', constructor)
    agents, default = discover_agents(config, ROOT / 'config.yaml', ROOT / 'models', ROOT / 'models')
    agent = agents[default]['agent']
    assert agent['source'] == 'local:qwen3-4b'
    assert agent['generation']['max_new_tokens'] == config['stage_settings']['model']['max_output_tokens']
    assert 'reasoning_effort' not in agents[default]['defaults']
    assert config == before and unify_model(config)['model'] == before['model']
    assert config['stage_settings']['plan']['output_schema'] == ACTION_SCHEMA
    constructor.assert_not_called()


def test_shared_storage_keeps_robot_markdown_and_conversation_rdf_isolated(tmp_path):
    """Run both workflows under one storage root and resume only conversation.

    Persist a complete robot cycle, then model attributed conversation messages
    with real RDF validation and atomic storage. Preserve all robot bytes while
    resuming the conversation, verify linked provenance and explicit roles, and
    reject conversational resumption of the robot run. Never create robot RDF.
    """
    from test_run_storage import run
    robot = run(root=tmp_path, sense=lambda directory, cycle: 'A visible cup.',
                model=lambda prompt, **settings: '# World Model\n\n## Entities\n\nA visible cup.')
    robot_bytes = {path.relative_to(robot): path.read_bytes() for path in robot.rglob('*') if path.is_file()}
    memory = open_conversation(tmp_path, configuration={'conversation': {'agent': 'test'}})
    namespace = 'urn:smpa:' + memory.metadata['run_id'] + ':'
    vocabulary = 'urn:smpa:belief#'

    def model(history, **settings):
        """Return an attributed proposition for the persisted message being modelled.

        The first inference asks for SDS; the second asks for typed triples.
        Preserve the supplied speaker as an individual and link its proposition,
        leaving source and stage provenance for the production validator to add.
        """
        payload = json.loads(history[1]['content'])
        if 'message' in payload:
            return {'response': json.dumps({'sds': payload['message']})}
        ordinal = payload['source'].rsplit('-', 1)[-1]
        speaker, claim = '#speaker_' + payload['speaker'], '#claim_' + ordinal
        triples = [dict(s=speaker, p=str(RDF.type), o={'uri': vocabulary + 'Entity'}),
                   dict(s=claim, p=str(RDF.type), o={'uri': vocabulary + 'Proposition'}),
                   dict(s=claim, p=vocabulary + 'text', o={'literal': payload['sds']}),
                   dict(s=claim, p=vocabulary + 'attributedTo', o={'uri': speaker})]
        return {'response': json.dumps({'triples': triples})}

    modelling = dict(generate=model, settings={}, agent={'backend': 'transformers', 'model_id': 'test-model'})
    answer = Mock(return_value={'response': 'That is your claim.', 'thinking': 'Explicit test trace'})
    try:
        exchange(memory, 'I believe a cup is blue.', answer, {}, modelling=modelling)
        graph = read_graph(memory.directory)
        assert len(graph) and all(str(subject).startswith(namespace) for subject in graph.subjects())
        assert set(graph.objects(None, DCTERMS.source))
        for source in graph.objects(None, DCTERMS.source):
            assert str(source).startswith('conversation.md#message-')
        for provenance in graph.objects(None, DCTERMS.provenance):
            record = json.loads((memory.directory / str(provenance)).read_text())
            assert record['stage'] == 'model' and record['status'] == 'succeeded'
        transcript = (memory.directory / 'conversation.md').read_bytes()
        memory.finalize('completed')
        memory = open_conversation(None, resume=memory.directory)
        exchange(memory, 'Continue.', answer, {}, modelling=modelling)
        assert set(graph) <= set(read_graph(memory.directory))
        assert (memory.directory / 'conversation.md').read_bytes().startswith(transcript)
        assert [message['role'] for message in read_messages(memory.directory)] == ['user', 'assistant'] * 2
        assert answer.call_args.args[0][0]['role'] == 'system'
        assert 'untrusted evidence' in answer.call_args.args[0][0]['content']
        assert len(list((tmp_path / 'runs').iterdir())) == 2
        assert not (robot / 'belief-state.ttl').exists()
        assert not list((memory.directory / 'world_model').glob('iteration_*.md'))
        assert all((robot / name).read_bytes() == content for name, content in robot_bytes.items())
        with pytest.raises(ValueError, match='Only conversational'):
            MemoryManager.resume_conversation(robot)
    finally:
        memory.finalize('completed')
