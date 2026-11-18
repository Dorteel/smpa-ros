"""Unit tests of the Dialogue's adapters: layout, Turtle reader, transcript reader, model catalogue, mock graphs."""
import json
import os
from pathlib import Path

import pytest

from smpa_gui.smpa_adapter import conversation_transcript, rdf_graphs, smpa_layout, smpa_models

GUI = Path(__file__).resolve().parent.parent
FIXTURE = GUI / 'test' / 'fixtures' / 'conversation'
MOCK = 'https://example.invalid/smpa-gui-mock/'


# ----- layout ------------------------------------------------------------------------------------------

def make_checkout(root, layout='packages'):
    """Create the few files that make a folder recognisable as an SMPA checkout of the given layout."""
    server = root / 'smpa_ros' / 'smpa_ros' / 'conversation_web.py' if layout == 'packages' else root / 'smpa_ros' / 'conversation_web.py'
    server.parent.mkdir(parents=True, exist_ok=True)
    server.write_text('# stand-in\n')
    return root


def test_layout_defaults_follow_the_checkout_layout_and_target_paths(tmp_path):
    """The packages layout puts the configuration in smpa_ros/; the flat one at the root; files prefer world_model/."""
    packages = smpa_layout.load(environment={'SMPA_GUI_SMPA_ROOT': str(make_checkout(tmp_path / 'new'))})
    root = (tmp_path / 'new').resolve()
    assert packages['smpa_layout'] == 'packages' and packages['config'] == root / 'smpa_ros' / 'config.yaml'
    assert packages['python'] == root / '.venv/bin/python' and packages['memory_root'] == root / 'memory'
    assert smpa_layout.python_path(root) == [root / 'smpa_ros', root / 'smpa_llm']
    flat = smpa_layout.load(environment={'SMPA_GUI_SMPA_ROOT': str(make_checkout(tmp_path / 'old', 'flat'))})
    assert flat['smpa_layout'] == 'flat' and flat['config'] == (tmp_path / 'old').resolve() / 'config.yaml'
    assert packages['files'] == {'conversation': ['world_model/conversation.md', 'conversation.md'],
                                 'sense': ['world_model/sense.ttl'],
                                 'belief': ['world_model/belief.ttl', 'belief-state.ttl'],
                                 'plan': ['world_model/plan.ttl']}
    assert packages['conversation_url'] is None and packages['conversation_port'] == 8766
    assert packages['mock_graphs'] == GUI / 'smpa_gui' / 'mock_graphs'


def test_without_a_checkout_nothing_is_assumed(tmp_path):
    """Away from an SMPA checkout (for example an installed package) the SMPA root stays unconfigured."""
    layout = smpa_layout.load(environment={'SMPA_GUI_SMPA_ROOT': str(tmp_path)})
    assert layout['smpa_layout'] is None and smpa_layout.describe(layout)['conversation_server_present'] is False
    assert smpa_layout.checkout_layout(None) is None


def test_layout_file_and_environment_override_defaults(tmp_path):
    """A layout file sets paths (one path or a list per file); individual environment variables win over it."""
    (tmp_path / 'layout.yaml').write_text('smpa_root: /opt/smpa\nconversation_port: 9001\nfiles:\n  belief: belief-state.ttl\n'
                                          '  plan: [plans/current.ttl, world_model/plan.ttl]\n')
    layout = smpa_layout.load(tmp_path / 'layout.yaml', {'SMPA_GUI_CONVERSATION_PORT': '9002', 'SMPA_GUI_MEMORY_ROOT': '/data/memory'})
    assert layout['smpa_root'] == Path('/opt/smpa') and layout['config'] == Path('/opt/smpa/config.yaml')
    assert layout['conversation_port'] == 9002 and layout['memory_root'] == Path('/data/memory')
    assert layout['files']['belief'] == ['belief-state.ttl'] and layout['files']['plan'] == ['plans/current.ttl', 'world_model/plan.ttl']
    (tmp_path / 'relative.yaml').write_text('smpa_root: smpa\nmock_graphs: fixtures\n')
    relative = smpa_layout.load(tmp_path / 'relative.yaml', {})
    assert relative['smpa_root'] == tmp_path.resolve() / 'smpa' and relative['mock_graphs'] == tmp_path.resolve() / 'fixtures'


@pytest.mark.parametrize('content, message', [
    ('unknown_key: 1\n', 'Unknown layout entries'),
    ('files:\n  belief: ../outside.ttl\n', 'stay inside'),
    ('files:\n  belief: /etc/passwd\n', 'stay inside'),
    ('files:\n  graph: x.ttl\n', 'files accepts only'),
    ('files:\n  belief: []\n', 'at least one'),
    ('files:\n  belief: [ok.ttl, ../bad.ttl]\n', 'stay inside'),
    ('- not a mapping\n', 'mapping'),
])
def test_layout_rejects_unknown_entries_and_paths_leaving_the_run(tmp_path, content, message):
    """Misspelt settings and per-run paths that escape the run directory are refused, not ignored."""
    (tmp_path / 'layout.yaml').write_text(content)
    with pytest.raises(smpa_layout.LayoutError, match=message):
        smpa_layout.load(tmp_path / 'layout.yaml', {})


def test_run_file_prefers_the_target_location_and_falls_back_to_smpas_current_one(tmp_path):
    """The first existing candidate is used; today's SMPA locations are fallbacks; escapes are refused."""
    run = tmp_path / 'run'
    run.mkdir()
    layout = smpa_layout.load(environment={})
    assert smpa_layout.run_file(run, layout, 'belief') == ((run / 'world_model' / 'belief.ttl').resolve(), 'world_model/belief.ttl', False)
    (run / 'belief-state.ttl').write_text('# No beliefs recorded yet\n')
    (run / 'conversation.md').write_text('# Conversation\n\n')
    assert smpa_layout.run_file(run, layout, 'belief') == ((run / 'belief-state.ttl').resolve(), 'belief-state.ttl', True)
    assert smpa_layout.run_file(run, layout, 'conversation')[1:] == ('conversation.md', True)
    (run / 'world_model').mkdir()
    (run / 'world_model' / 'belief.ttl').write_text('')
    assert smpa_layout.run_file(run, layout, 'belief')[1:] == ('world_model/belief.ttl', False)
    (run / 'world_model' / 'belief.ttl').unlink()
    (run / 'world_model').rmdir()
    (run / 'world_model').symlink_to(tmp_path)
    with pytest.raises(smpa_layout.LayoutError):
        smpa_layout.run_file(run, layout, 'belief')


# ----- Turtle reader -----------------------------------------------------------------------------------

TURTLE = """@prefix ex: <http://example.org/test#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:a a ex:Thing ; rdfs:label "A" ; ex:near ex:b , [ ex:size 3 ] .
ex:b rdfs:label "B"@en .
"""


def test_rdf_reader_turns_triples_into_nodes_edges_and_literal_values(tmp_path):
    """Resources become nodes, resource triples edges, literals stay with their subject; no vocabulary is special."""
    path = tmp_path / 'g.ttl'
    path.write_text(TURTLE)
    graph = rdf_graphs.read(path)
    assert graph['state'] == 'ok' and graph['triples'] == 6 and not graph['truncated']
    nodes = {node['label']: node for node in graph['nodes']}
    assert {'ex:a', 'ex:b', 'ex:Thing'} <= set(nodes)
    assert sum(node['blank'] for node in graph['nodes']) == 1
    assert {edge['label'] for edge in graph['edges']} == {'rdf:type', 'ex:near'}
    assert {(item['predicate_label'], item['value']) for item in nodes['ex:a']['literals']} == {('rdfs:label', 'A')}
    assert nodes['ex:b']['literals'][0]['language'] == 'en'
    blank = next(node for node in graph['nodes'] if node['blank'])
    assert blank['literals'][0]['datatype'].endswith('#integer')


def test_rdf_reader_reports_missing_empty_malformed_unavailable_and_changes(tmp_path):
    """Every file state is reported, and the revision changes exactly when the content changes."""
    path = tmp_path / 'g.ttl'
    assert rdf_graphs.read(path)['state'] == 'missing'
    path.write_text('# No beliefs recorded yet\n')
    assert rdf_graphs.read(path)['state'] == 'empty'
    path.write_text('@prefix ex: <http://example.org/> .\nex:a ex:b ')
    malformed = rdf_graphs.read(path)
    assert malformed['state'] == 'malformed' and malformed['error'] and malformed['nodes'] == []
    path.write_text(TURTLE)
    first = rdf_graphs.read(path)['revision']
    assert rdf_graphs.read(path)['revision'] == first
    path.write_text(TURTLE + 'ex:c ex:near ex:a .\n')
    assert rdf_graphs.read(path)['revision'] != first
    folder = tmp_path / 'folder.ttl'
    folder.mkdir()
    assert rdf_graphs.read(folder)['state'] == 'unavailable'


def test_rdf_reader_truncates_large_graphs(tmp_path, monkeypatch):
    """Graphs above the limit are cut and marked truncated rather than freezing the browser."""
    monkeypatch.setattr(rdf_graphs, 'MAX_TRIPLES', 3)
    path = tmp_path / 'g.ttl'
    path.write_text(TURTLE)
    graph = rdf_graphs.read(path)
    assert graph['truncated'] and graph['triples'] == 6
    assert len(graph['edges']) + sum(len(node['literals']) for node in graph['nodes']) == 3


def test_mock_graphs_are_valid_and_unmistakably_mock():
    """Each mock view parses, uses only the reserved mock namespace for its subjects, and says it is mock data."""
    for view in smpa_layout.GRAPH_VIEWS:
        path = GUI / 'smpa_gui' / 'mock_graphs' / f'{view}.ttl'
        graph = rdf_graphs.read(path)
        assert graph['state'] == 'ok', view
        subjects = {node['iri'] for node in graph['nodes'] if node['literals'] or any(edge['source'] == node['id'] for edge in graph['edges'])}
        assert all(iri.startswith(MOCK) for iri in subjects), view
        assert 'MOCK DATA' in path.read_text().splitlines()[0]
        assert any('Mock' in item['value'] and 'Not recorded by SMPA' in item['value']
                   for node in graph['nodes'] for item in node['literals']), view


# ----- transcript reader --------------------------------------------------------------------------------

def entry(role, body, agent=None, parts=None, number=1):
    """Write one transcript entry in SMPA's format (as smpa_ros.conversation.append_message does)."""
    data = body.encode()
    suffix = ''
    if parts is not None:
        thinking, response = parts
        suffix = f' parts {len(thinking.encode())} {len(response.encode())}'
        data = b'<response>\n' + response.encode() + b'\n</response>'
        if thinking:
            data = b'<thinking>\n' + thinking.encode() + b'\n</thinking>\n\n' + data
    metadata = ('<!-- smpa-agent ' + json.dumps(agent) + ' -->').encode() if agent else b''
    head = f'<!-- smpa-message {role} {len(data)}' + (f' agent {len(metadata)}' if metadata else '') + suffix + ' -->\n'
    return head.encode() + (metadata + b'\n' if metadata else b'') + f'<a id="message-{number}"></a>\n## {role}\n\n'.encode() + data + b'\n\n'


def test_transcript_reader_reads_messages_agents_and_reasoning(tmp_path):
    """User and assistant messages, agent provenance and the thinking part are read without interpretation."""
    agent = {'source': 'api:stub', 'label': 'API · stub', 'model_id': 'stub-model', 'endpoint': 'http://x', 'provider': 'stub'}
    data = (conversation_transcript.HEADER + entry('User', 'Hello <b>robot</b>\n\n## not a heading') +
            entry('Assistant', '', agent=agent, parts=('I think.', 'Hi.'), number=2))
    (tmp_path / 'conversation.md').write_bytes(data)
    result = conversation_transcript.read(tmp_path / 'conversation.md')
    assert result['state'] == 'ok' and result['problem'] is None
    user, assistant = result['messages']
    assert user == dict(index=1, role='user', content='Hello <b>robot</b>\n\n## not a heading', thinking=None, agent=None)
    assert assistant['content'] == 'Hi.' and assistant['thinking'] == 'I think.'
    assert assistant['agent']['source'] == 'api:stub' and 'endpoint' not in assistant['agent']


def test_transcript_reader_keeps_complete_messages_before_an_incomplete_one(tmp_path):
    """A writer midway through an entry does not hide the messages before it; missing and foreign files are explained."""
    complete = conversation_transcript.HEADER + entry('User', 'First')
    (tmp_path / 'conversation.md').write_bytes(complete + entry('Assistant', 'Second reply', number=2)[:-12])
    result = conversation_transcript.read(tmp_path / 'conversation.md')
    assert result['state'] == 'partial' and [m['content'] for m in result['messages']] == ['First'] and result['problem']
    assert conversation_transcript.read(tmp_path / 'absent.md')['state'] == 'missing'
    (tmp_path / 'other.md').write_text('# Something else\n')
    assert conversation_transcript.read(tmp_path / 'other.md')['state'] == 'partial'


# ----- model catalogue ----------------------------------------------------------------------------------

def test_model_catalogue_follows_smpa_selection_rules_and_explains_availability(tmp_path):
    """Local text models and API providers with text models are listed; descriptions use configuration facts only."""
    models, problems = smpa_models.catalogue(FIXTURE / 'config.yaml', FIXTURE / 'models', environment={})
    assert problems == []
    by_source = {model['source']: model for model in models}
    assert set(by_source) == {'local:absent-local-model', 'api:stub'}
    assert by_source['api:stub']['default'] and by_source['api:stub']['modelling_agent']
    assert by_source['api:stub']['availability']['state'] == 'unavailable'
    assert 'SMPA_GUI_STUB_API_KEY' in by_source['api:stub']['availability']['detail']
    assert by_source['local:absent-local-model']['availability']['state'] == 'unavailable'
    assert 'stub-model' in by_source['api:stub']['description']
    configured, _ = smpa_models.catalogue(FIXTURE / 'config.yaml', FIXTURE / 'models', environment={'SMPA_GUI_STUB_API_KEY': 'x'})
    stub = next(model for model in configured if model['source'] == 'api:stub')
    assert stub['availability']['state'] == 'likely' and stub['availability']['verified'] is False


def test_model_catalogue_reads_dotenv_presence_and_reports_unusable_configuration(tmp_path, monkeypatch):
    """A key in .env beside the configuration counts; an unknown default agent and unreadable files are reported.

    No smpa_llm package is visible here, as in a workspace that was not
    sourced, so the missing registry beside the configuration is reported.
    """
    monkeypatch.setattr(smpa_models, 'package_share', lambda name: None)
    (tmp_path / 'models').mkdir()
    (tmp_path / 'config.yaml').write_text((FIXTURE / 'config.yaml').read_text().replace('agent: api:stub', 'agent: api:absent', 1))
    (tmp_path / '.env').write_text('SMPA_GUI_STUB_API_KEY="secret-value"\n')
    models, problems = smpa_models.catalogue(tmp_path / 'config.yaml', environment={})
    assert [model['source'] for model in models] == ['api:stub'] and models[0]['availability']['state'] == 'likely'
    assert 'secret-value' not in json.dumps(models)
    assert any('model_library.yaml' in problem for problem in problems)
    assert any('api:absent' in problem for problem in problems)
    assert smpa_models.catalogue(tmp_path / 'missing.yaml')[0] == []


def packaged_checkout(root, selector='package://smpa_llm/config.yaml'):
    """Write a minimal SMPA checkout in the ROS 2 packages layout: cognitive config, smpa_llm defaults and registry."""
    make_checkout(root)
    (root / 'smpa_ros' / 'config.yaml').write_text(f'inference_config: {selector}\nconversation:\n  agent: local:tiny\n'
                                                  '  modelling_agent: local:tiny\n')
    (root / 'smpa_llm' / 'models').mkdir(parents=True)
    (root / 'smpa_llm' / 'config.yaml').write_text('openai:\n  base_url: https://api.example.invalid/v1\n  api_key_env: EXAMPLE_KEY\n'
                                                  '  models:\n    text: example-text\nnebula:\n  base_url: x\n  models:\n    vision: v\n')
    (root / 'smpa_llm' / 'models' / 'model_library.yaml').write_text(
        'models:\n  tiny:\n    backend: transformers\n    model_id: example/tiny\n    path: text/tiny\n'
        '  whisper:\n    backend: faster_whisper\n    model_id: example/whisper\n')
    return root


def test_catalogue_follows_the_packaged_configuration_split(tmp_path, monkeypatch):
    """inference_config takes providers from smpa_llm; the registry comes from smpa_llm when none is beside the config."""
    monkeypatch.setattr(smpa_models, 'package_share', lambda name: None)
    root = packaged_checkout(tmp_path / 'smpa')
    models, problems = smpa_models.catalogue(root / 'smpa_ros' / 'config.yaml', root / 'models', environment={}, smpa_root=root)
    assert [model['source'] for model in models] == ['local:tiny', 'api:openai']
    assert next(model for model in models if model['source'] == 'local:tiny')['default'] is True
    assert any('not installed in the sourced workspace' in problem for problem in problems) and len(problems) == 1
    monkeypatch.setattr(smpa_models, 'package_share', lambda name: root / 'smpa_llm' if name == 'smpa_llm' else None)
    assert smpa_models.catalogue(root / 'smpa_ros' / 'config.yaml', root / 'models', environment={}, smpa_root=root)[1] == []
    relative = packaged_checkout(tmp_path / 'relative', selector='../smpa_llm/config.yaml')
    models, _ = smpa_models.catalogue(relative / 'smpa_ros' / 'config.yaml', environment={'EXAMPLE_KEY': 'k'}, smpa_root=relative)
    assert next(model for model in models if model['source'] == 'api:openai')['availability']['state'] == 'likely'
    bad = packaged_checkout(tmp_path / 'bad', selector='package://elsewhere/config.yaml')
    models, problems = smpa_models.catalogue(bad / 'smpa_ros' / 'config.yaml', environment={}, smpa_root=bad)
    assert models == [] and 'unsupported inference_config' in problems[-1]


def test_catalogue_matches_the_real_smpa_checkout_when_available():
    """Against the real SMPA files (SMPA_GUI_TEST_SMPA_ROOT), the GUI lists exactly the registry's text models and providers."""
    import yaml
    from conftest import SMPA_SOURCE
    if smpa_layout.checkout_layout(SMPA_SOURCE) != 'packages':
        pytest.skip('no SMPA checkout in the ROS 2 packages layout')
    layout = smpa_layout.load(environment={'SMPA_GUI_SMPA_ROOT': str(SMPA_SOURCE)})
    models, _ = smpa_models.catalogue(layout['config'], layout['model_root'], environment={}, smpa_root=SMPA_SOURCE)
    registry = yaml.safe_load((SMPA_SOURCE / 'smpa_llm' / 'models' / 'model_library.yaml').read_text())['models']
    providers = yaml.safe_load((SMPA_SOURCE / 'smpa_llm' / 'config.yaml').read_text())
    expected = {f'local:{name}' for name, item in registry.items() if item.get('backend') in ('transformers', 'ollama')}
    expected |= {f'api:{name}' for name, item in providers.items() if isinstance(item, dict) and (item.get('models') or {}).get('text')
                 and item.get('base_url') and item.get('api_key_env')}
    assert {model['source'] for model in models} == expected
    assert next(model for model in models if model['default'])['source'] == 'local:qwen3-4b'


def test_server_verdicts_replace_prechecks():
    """Once SMPA's server runs, its availability and agent id are shown instead of the GUI's precheck."""
    models, _ = smpa_models.catalogue(FIXTURE / 'config.yaml', FIXTURE / 'models', environment={})
    merged = smpa_models.merge_server_agents(models, [{'source': 'api:stub', 'id': 'api:stub:1', 'available': True,
                                                       'availability': 'Loaded on first message.', 'label': 'API · stub'}])
    stub = next(model for model in merged if model['source'] == 'api:stub')
    assert stub['availability'] == dict(state='available', verified=True, detail='Loaded on first message.')
    assert stub['agent_id'] == 'api:stub:1'


def test_conversation_server_prerequisites_explain_what_is_missing(tmp_path, monkeypatch):
    """Without a checkout, interpreter, configuration or installed smpa_llm, starting is refused with the reason."""
    from smpa_gui.smpa_adapter.conversation_service import ConversationService
    unconfigured = dict(smpa_layout.load(environment={}), smpa_root=None, smpa_layout=None)
    assert 'an SMPA checkout' in ConversationService(unconfigured).prerequisites()[0]
    root = packaged_checkout(tmp_path / 'smpa')
    layout = smpa_layout.load(environment={'SMPA_GUI_SMPA_ROOT': str(root)})
    assert any('Python interpreter' in item for item in ConversationService(layout).prerequisites())
    (root / '.venv' / 'bin').mkdir(parents=True)
    (root / '.venv' / 'bin' / 'python').write_text('')
    monkeypatch.setattr(smpa_models, 'package_share', lambda name: None)
    assert any('smpa_llm in the sourced workspace' in item for item in ConversationService(layout).prerequisites())
    monkeypatch.setattr(smpa_models, 'package_share', lambda name: root / 'smpa_llm')
    assert ConversationService(layout).prerequisites() == []
