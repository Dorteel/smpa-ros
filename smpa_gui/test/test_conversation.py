"""Tests of the conversation and graph routes against SMPA's real conversation server and the inference stub.

The GUI server starts SMPA's own ``smpa_ros.conversation_web`` from the SMPA
checkout named by ``SMPA_GUI_TEST_SMPA_ROOT`` (default: the checkout that
contains smpa_gui/), with a test configuration whose only API provider is the
local stub. Everything is written to temporary directories.
"""
import json
import signal
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

from conftest import (GUI, PYTHON, SMPA_SOURCE, conversation_world, fetch, free_port, ros_available,
                      smpa_conversation_available, start_server, stop)
from test_smpa_runs import tree_fingerprint

API = '/api/v1/smpa'


def token(base):
    """Fetch the GUI session token that conversation changes require."""
    return fetch(base + '/api/v1/actions/session')['token']


def post(base, path, body, token_value='', content_type='application/json', timeout=120):
    """POST JSON to the GUI server and return (status, body) without raising on HTTP errors."""
    request = Request(base + API + path, data=json.dumps(body).encode(), method='POST',
                      headers={'Content-Type': content_type, 'X-SMPA-GUI-Token': token_value})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def rid(name):
    """Make a valid request id from a short name."""
    return f'test-{name}-0001'


def run_directory(world, run_id):
    """Find a run directory by run id in the test memory root."""
    return next(path for path in (world['memory'] / 'runs').iterdir() if path.name.endswith(run_id))


def metadata(world, run_id):
    """Read a run's metadata.yaml."""
    return yaml.safe_load((run_directory(world, run_id) / 'metadata.yaml').read_text())


@pytest.fixture(scope='module')
def world(conversation_server):
    """The module's GUI server with a conversation world; adds the session token."""
    conversation_server['token'] = token(conversation_server['base'])
    return conversation_server


def test_layout_and_models_before_any_conversation(world):
    """The layout and the model choices are shown without starting SMPA's server (which would create a run)."""
    layout = fetch(world['base'] + API + '/layout')['layout']
    assert layout['files']['belief'] == ['world_model/belief.ttl', 'belief-state.ttl'] and layout['conversation_server_present']
    answer = fetch(world['base'] + API + '/models')
    assert answer['verified_by'] == 'gui_precheck' and answer['problems'] == []
    by_source = {model['source']: model for model in answer['models']}
    assert by_source['api:stub']['default'] and by_source['api:stub']['availability']['state'] == 'likely'
    assert by_source['local:absent-local-model']['availability']['state'] == 'unavailable'
    status = fetch(world['base'] + API + '/conversation')['conversation']
    assert status['phase'] == 'stopped' and status['run_id'] is None and status['prerequisites_missing'] == []
    assert not list((world['memory'] / 'runs').iterdir())


def test_conversation_changes_require_the_local_session_token_and_json(world):
    """Without the token, or without a JSON body, nothing is started."""
    assert post(world['base'], '/conversation/start', {'request_id': rid('notoken')})[0] == 403
    assert post(world['base'], '/conversation/start', {'request_id': rid('form')}, world['token'], 'text/plain')[0] in (415, 422)
    request = Request(world['base'] + API + '/conversation/start', data=b'{"request_id":"test-origin-0001"}', method='POST',
                      headers={'Content-Type': 'application/json', 'X-SMPA-GUI-Token': world['token'], 'Origin': 'http://evil.example'})
    with pytest.raises(HTTPError) as refused:
        urlopen(request, timeout=10)
    assert refused.value.code == 403
    assert not list((world['memory'] / 'runs').iterdir())


def test_start_select_send_and_save(world):
    """Starting creates an SMPA run with the chosen model; a message is answered and saved in the run's transcript."""
    status, body = post(world['base'], '/conversation/start', {'source': 'api:stub', 'request_id': rid('start')}, world['token'])
    assert status == 200, body
    conversation = body['conversation']
    assert conversation['phase'] == 'ready' and conversation['active_agent']['source'] == 'api:stub'
    run_id = world['first_run'] = conversation['run_id']
    again = post(world['base'], '/conversation/start', {'source': 'api:stub', 'request_id': rid('start')}, world['token'])[1]
    assert again['conversation']['run_id'] == run_id and len(list((world['memory'] / 'runs').iterdir())) == 1
    sessions = fetch(world['base'] + API + '/sessions')['sessions']
    assert any(item['session_id'] == run_id and item['kind'] == 'conversation' for item in sessions)
    models = fetch(world['base'] + API + '/models')
    assert models['verified_by'] == 'conversation_server'
    assert next(m for m in models['models'] if m['source'] == 'api:stub')['availability']['state'] == 'available'

    status, body = post(world['base'], '/conversation/messages', {'text': 'Hello robot', 'request_id': rid('hello')}, world['token'])
    assert status == 200, body
    assert [m['content'] for m in body['conversation']['messages']] == ['Hello robot', 'Stub reply 1 to: Hello robot']
    assert body['conversation']['messages'][1]['agent']['source'] == 'api:stub'
    repeat = post(world['base'], '/conversation/messages', {'text': 'Hello robot', 'request_id': rid('hello')}, world['token'])[1]
    assert len(repeat['conversation']['messages']) == 2
    transcript = (run_directory(world, run_id) / 'conversation.md').read_text()
    assert 'Hello robot' in transcript and 'Stub reply 1' in transcript
    recorded = fetch(world['base'] + API + f'/sessions/{run_id}/conversation')
    assert recorded['state'] == 'ok' and recorded['active'] and len(recorded['messages']) == 2
    assert recorded['configured_path'] == 'conversation.md'


def test_inference_failure_keeps_the_saved_message_and_explains(world):
    """When inference fails, SMPA keeps the user's message; the GUI passes on SMPA's explanation with the state."""
    status, body = post(world['base'], '/conversation/messages', {'text': 'FAIL-INFERENCE please', 'request_id': rid('fail')}, world['token'])
    assert status == 502 and 'Your message is saved' in body['detail']
    assert body['conversation']['messages'][-1]['content'] == 'FAIL-INFERENCE please'
    assert body['conversation']['phase'] == 'ready'


def test_unavailable_model_is_refused_by_smpa(world):
    """Selecting a model whose weights are missing is refused with SMPA's reason; the active model stays."""
    status, body = post(world['base'], '/conversation/model', {'source': 'local:absent-local-model'}, world['token'])
    assert status == 409 and 'weights missing' in body['detail']
    assert body['conversation']['active_agent']['source'] == 'api:stub'
    assert post(world['base'], '/conversation/model', {'source': 'api:nothing'}, world['token'])[0] == 404


def test_new_conversation_then_reopen_and_continue_the_first(world):
    """A second conversation is a new run; the first can be reopened from its record and continued."""
    first = world['first_run']
    second = post(world['base'], '/conversation/start', {'source': 'api:stub', 'request_id': rid('second')}, world['token'])[1]
    second_id = second['conversation']['run_id']
    assert second_id != first and second['conversation']['messages'] == []
    assert metadata(world, first)['status'] == 'completed'
    recorded = fetch(world['base'] + API + f'/sessions/{first}/conversation')
    assert not recorded['active'] and len(recorded['messages']) == 3
    status, body = post(world['base'], '/conversation/resume', {'session_id': first, 'request_id': rid('resume')}, world['token'])
    assert status == 200, body
    assert body['conversation']['run_id'] == first and len(body['conversation']['messages']) == 3
    body = post(world['base'], '/conversation/messages', {'text': 'Back again', 'request_id': rid('again')}, world['token'])[1]
    user, reply = [m['content'] for m in body['conversation']['messages']][-2:]
    assert user == 'Back again' and reply.startswith('Stub reply') and reply.endswith('to: Back again')
    assert 'Back again' in (run_directory(world, first) / 'conversation.md').read_text()
    assert post(world['base'], '/conversation/resume', {'session_id': 'no-such-run', 'request_id': rid('nosuch')}, world['token'])[0] == 404


def test_graph_files_are_read_from_the_configured_paths_in_every_state(world):
    """Each view reads its configured file in the session's run; missing, empty, malformed, unreadable and changes are reported.

    Before anything is written to world_model/, Belief shows the graph SMPA
    really writes today (belief-state.ttl at the run root, here with the
    stub's modelled triple) and says that it is the fallback location.
    """
    run = run_directory(world, world['first_run'])
    url = world['base'] + API + '/graphs/{}?session_id=' + world['first_run']
    for view in ('sense', 'plan'):
        graph = fetch(url.format(view))
        assert graph['state'] == 'missing' and graph['configured_path'] == f'world_model/{view}.ttl' and not graph['mock']
        assert graph['path'] == str((run / 'world_model' / f'{view}.ttl').resolve()) and not graph['fallback']
    current = fetch(url.format('belief'))
    assert current['configured_path'] == 'belief-state.ttl' and current['fallback'] is True
    assert current['state'] == 'ok' and any(node['id'].endswith('stub_speaker') for node in current['nodes'])
    target = run / 'world_model' / 'belief.ttl'
    target.write_text('@prefix ex: <http://example.org/> .\nex:cup ex:on ex:table .\n')
    first = fetch(url.format('belief'))
    assert first['state'] == 'ok' and len(first['nodes']) == 2 and len(first['edges']) == 1
    assert first['configured_path'] == 'world_model/belief.ttl' and first['fallback'] is False
    target.write_text('@prefix ex: <http://example.org/> .\nex:cup ex:on ex:table .\nex:table ex:in ex:room .\n')
    second = fetch(url.format('belief'))
    assert second['revision'] != first['revision'] and len(second['nodes']) == 3
    assert fetch(url.format('sense'))['state'] == 'missing' and fetch(url.format('plan'))['state'] == 'missing'
    target.write_text('# nothing yet\n')
    assert fetch(url.format('belief'))['state'] == 'empty'
    target.write_text('@prefix ex: <http://example.org/> .\nex:cup ex:on ')
    assert fetch(url.format('belief'))['state'] == 'malformed'
    target.unlink()
    target.mkdir()
    assert fetch(url.format('belief'))['state'] == 'unavailable'
    target.rmdir()
    with pytest.raises(HTTPError) as unknown:
        fetch(world['base'] + API + '/graphs/model?session_id=' + world['first_run'])
    assert unknown.value.code == 404


def test_mock_graphs_are_marked_and_never_written_into_runs(world):
    """Mock views come from the GUI's fixtures, are marked mock, and leave every run directory unchanged."""
    before = tree_fingerprint(world['memory'])
    for view in ('sense', 'belief', 'plan'):
        graph = fetch(world['base'] + API + f'/graphs/{view}?source=mock&session_id=' + world['first_run'])
        assert graph['state'] == 'ok' and graph['mock'] and graph['source'] == 'mock'
        assert graph['path'] == str(GUI / 'smpa_gui' / 'mock_graphs' / f'{view}.ttl')
        assert all(node['iri'].startswith('https://example.invalid/smpa-gui-mock/') or not node['literals'] for node in graph['nodes'])
    assert tree_fingerprint(world['memory']) == before


def test_stop_finalizes_the_run_and_gui_exit_stops_the_server(world):
    """Stopping ends SMPA's server cleanly; stopping the GUI stops a server it started."""
    status, body = post(world['base'], '/conversation/stop', {}, world['token'])
    assert status == 200 and body['conversation']['phase'] == 'stopped'
    assert metadata(world, world['first_run'])['status'] in ('completed', 'interrupted')
    assert post(world['base'], '/conversation/messages', {'text': 'Anyone?', 'request_id': rid('nobody')}, world['token'])[0] == 409
    body = post(world['base'], '/conversation/start', {'request_id': rid('third')}, world['token'])[1]
    third = body['conversation']['run_id']
    pid = body['conversation']['pid']
    stop(world['process'])
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        import os
        os.kill(pid, 0)
    assert metadata(world, third)['status'] in ('completed', 'interrupted')


def test_read_only_gui_refuses_conversation_changes(tmp_path):
    """With --read-only (SMPA_GUI_READ_ONLY=1) nothing can be started; models and graphs remain readable."""
    if not ros_available():
        pytest.skip('ROS 2 with rclpy and the GUI packages is required')
    world = conversation_world(tmp_path)
    world['environment']['SMPA_GUI_READ_ONLY'] = '1'
    process, base = start_server(world['environment'])
    try:
        assert fetch(base + '/api/v1/actions/session')['enabled'] is False
        status, body = post(base, '/conversation/start', {'request_id': rid('readonly')}, '')
        assert status == 403 and 'read-only' in body['detail']
        assert fetch(base + API + '/models')['models']
        assert fetch(base + API + '/graphs/belief?source=mock')['state'] == 'ok'
    finally:
        stop(process)
        stop(world['stub'])


def test_server_start_failure_is_explained_with_smpa_output(tmp_path):
    """If SMPA's server refuses to start (here: unknown configured agent), the GUI reports its output; nothing hangs."""
    if not ros_available() or not smpa_conversation_available():
        pytest.skip('ROS 2 and an SMPA checkout with .venv are required')
    world = conversation_world(tmp_path, agent='api:absent')
    process, base = start_server(world['environment'])
    try:
        assert any('api:absent' in problem for problem in fetch(base + API + '/models')['problems'])
        status, body = post(base, '/conversation/start', {'request_id': rid('broken')}, token(base))
        assert status == 502 and 'exited with code' in body['detail']
        assert body['conversation']['phase'] == 'failed' and body['conversation']['log']
    finally:
        stop(process)
        stop(world['stub'])


def test_external_conversation_server_is_used_and_never_stopped(tmp_path):
    """With conversation_url the GUI relays to a server it did not start, and leaves it running."""
    if not ros_available() or not smpa_conversation_available():
        pytest.skip('ROS 2 and an SMPA checkout with .venv are required')
    world = conversation_world(tmp_path)
    port = free_port()
    from smpa_gui.smpa_adapter.smpa_layout import python_path
    environment = dict(world['environment'], PYTHONPATH=':'.join(str(path) for path in python_path(SMPA_SOURCE)))
    external = subprocess.Popen([str(SMPA_SOURCE / '.venv/bin/python'), '-m', 'smpa_ros.conversation_web',
                                 '--config', str(tmp_path / 'config.yaml'), '--world-model-root', str(world['memory']),
                                 '--model-root', str(tmp_path / 'models'), '--port', str(port)],
                                cwd=SMPA_SOURCE, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    world['environment']['SMPA_GUI_CONVERSATION_URL'] = f'http://127.0.0.1:{port}'
    process = None
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                fetch(f'http://127.0.0.1:{port}/api/conversation')
                break
            except OSError:
                assert external.poll() is None and time.monotonic() < deadline, external.stdout.read()
                time.sleep(0.3)
        process, base = start_server(world['environment'])
        status = fetch(base + API + '/conversation')['conversation']
        assert status['mode'] == 'external' and status['phase'] == 'ready' and status['run_id']
        body = post(base, '/conversation/messages', {'text': 'Via external', 'request_id': rid('external')}, token(base))[1]
        assert body['conversation']['messages'][-1]['content'] == 'Stub reply 1 to: Via external'
        assert post(base, '/conversation/stop', {}, token(base))[0] == 200
        stop(process)
        process = None
        assert external.poll() is None
    finally:
        if process is not None:
            stop(process)
        external.send_signal(signal.SIGINT)
        external.wait(15)
        stop(world['stub'])


def test_real_smpa_configuration_lists_the_same_models_as_smpa(tmp_path):
    """With SMPA's own configuration (packages layout), the GUI's model list equals the agents SMPA's server offers.

    Uses the checkout's real ``smpa_ros/config.yaml`` (inference defaults
    from the installed smpa_llm package) with a temporary memory directory.
    No message is sent, so no model is loaded or contacted.
    """
    from smpa_gui.smpa_adapter import smpa_layout, smpa_models
    if not ros_available() or smpa_layout.checkout_layout(SMPA_SOURCE) != 'packages' or not smpa_conversation_available():
        pytest.skip('needs ROS 2 and an SMPA checkout in the ROS 2 packages layout with its .venv')
    if smpa_models.package_share('smpa_llm') is None:
        pytest.skip('smpa_llm is not installed in the sourced workspace (source the SMPA workspace)')
    import yaml
    (tmp_path / 'memory' / 'runs').mkdir(parents=True)
    (tmp_path / 'layout.yaml').write_text(yaml.safe_dump(dict(smpa_root=str(SMPA_SOURCE), memory_root=str(tmp_path / 'memory'),
                                                              conversation_port=free_port())))
    from conftest import isolated_environment
    environment = isolated_environment()
    environment.pop('SMPA_GUI_MEMORY_ROOT', None)
    environment['SMPA_GUI_LAYOUT'] = str(tmp_path / 'layout.yaml')
    process, base = start_server(environment)
    try:
        before = fetch(base + API + '/models')
        assert before['config'] == str(SMPA_SOURCE / 'smpa_ros' / 'config.yaml') and before['problems'] == []
        status = fetch(base + API + '/conversation')['conversation']
        assert status['prerequisites_missing'] == []
        code, body = post(base, '/conversation/start', {'request_id': rid('realcfg')}, token(base))
        assert code == 200, body
        agents = {agent['source'] for agent in body['conversation']['agents']}
        after = fetch(base + API + '/models')
        assert {model['source'] for model in before['models']} == agents
        assert after['verified_by'] == 'conversation_server' and all(model['agent_id'] for model in after['models'])
        assert body['conversation']['active_agent']['source'] == next(model['source'] for model in before['models'] if model['default'])
        assert post(base, '/conversation/stop', {}, token(base))[0] == 200
    finally:
        stop(process)
