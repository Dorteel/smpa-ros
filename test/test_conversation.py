"""Conversation tests run with unittest without ROS, or in the repository suite."""
import json
import os
import shutil
from pathlib import Path
import tempfile
import subprocess
import sys
import select
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

import yaml

from smpa_ros.conversation import exchange, open_conversation, read_messages
from smpa_ros.conversation_web import make_server, discover_agents, restore_agent, select_agent


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.memory = open_conversation(Path(self.temporary.name))
        self.addCleanup(lambda: self.memory.finalize('completed'))
        self.generate = Mock(return_value='The cup is red.')

    def test_creation_and_multiple_exchanges(self):
        root = self.memory.directory
        self.assertTrue((root / 'goal.md').is_file())
        self.assertEqual(read_messages(root), [])
        exchange(self.memory, 'There is a red cup.', self.generate, {'max_new_tokens': 128})
        exchange(self.memory, 'What color is it?', self.generate, {'max_new_tokens': 128})
        history = read_messages(root)
        self.assertEqual([m['role'] for m in history], ['user', 'assistant', 'user', 'assistant'])
        self.assertEqual(self.generate.call_args.args[0], [
            {'role': 'user', 'content': 'There is a red cup.'},
            {'role': 'assistant', 'content': 'The cup is red.'},
            {'role': 'user', 'content': 'What color is it?'},
        ])
        transcript = (root / 'conversation.md').read_text()
        self.assertLess(transcript.index('## User'), transcript.index('## Assistant'))
        self.assertLess(transcript.index('There is a red cup.'), transcript.index('What color is it?'))
        self.assertEqual(self.memory.metadata['completed_cycles'], 2)

    def test_failure_preserves_user_and_can_continue(self):
        def failure(prompt, **settings):
            self.assertEqual(read_messages(self.memory.directory)[-1]['content'], 'Remember this.')
            raise RuntimeError('mock failure')
        with self.assertRaisesRegex(RuntimeError, 'mock failure'):
            exchange(self.memory, 'Remember this.', failure, {})
        self.assertEqual(self.memory.metadata['completed_cycles'], 0)
        record = json.loads((self.memory.directory / 'state/cycle_000001/act.json').read_text())
        self.assertEqual(record['status'], 'failed')
        exchange(self.memory, 'Continue.', self.generate, {})
        self.assertEqual(self.generate.call_args.args[0][0], {'role': 'user', 'content': 'Remember this.'})
        self.assertEqual(len(read_messages(self.memory.directory)), 3)

    def test_reload_preserves_arbitrary_markdown_and_provenance(self):
        text = 'Unicode: ☕\n\n## Assistant\n\n<!-- smpa-message User 7 -->\n'
        exchange(self.memory, text, self.generate, {})
        before = (self.memory.directory / 'conversation.md').read_bytes()
        self.memory.finalize('interrupted')
        self.memory = open_conversation(None, resume=self.memory.directory)
        self.assertEqual(read_messages(self.memory.directory)[0]['content'], text)
        exchange(self.memory, 'Next', self.generate, {})
        self.assertTrue((self.memory.directory / 'conversation.md').read_bytes().startswith(before))
        self.assertEqual(self.memory.metadata['completed_cycles'], 2)
        record = json.loads((self.memory.directory / 'state/cycle_000002/sense.json').read_text())
        self.assertEqual(record['sequence'], 3)

    def test_empty_response_and_corruption_are_not_overwritten(self):
        with self.assertRaises(ValueError):
            exchange(self.memory, 'Hello', lambda *a, **k: '', {})
        transcript = self.memory.directory / 'conversation.md'
        with transcript.open('ab') as output:
            output.write(b'partial entry')
        before = transcript.read_bytes()
        with self.assertRaises(ValueError):
            open_conversation(None, resume=self.memory.directory)
        self.assertEqual(before, transcript.read_bytes())

    def test_http_gui_success_failure_and_validation(self):
        server = make_server(self.memory, self.generate, {}, port=0)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        base = f'http://127.0.0.1:{server.server_port}'
        with urlopen(base) as response:
            self.assertIn(b'Belief graph', response.read())
        def post(message, **headers):
            return urlopen(Request(base + '/api/message', data=json.dumps({'message': message}).encode(),
                                   headers={'Content-Type': 'application/json', **headers}))
        with post('Hello') as response:
            state = json.load(response)
        self.assertEqual(len(state['messages']), 2)
        self.assertEqual(state['graph'], {'nodes': [], 'edges': []})
        self.generate.side_effect = RuntimeError('private backend details')
        with self.assertRaises(HTTPError) as raised:
            post('Another message')
        self.assertEqual(raised.exception.code, 500)
        state = json.load(raised.exception)
        self.assertEqual(len(state['messages']), 3)
        self.assertNotIn('private backend details', state['error'])
        for message, headers, code in [('', {}, 400), ('X', {'Origin': 'https://other.example'}, 403)]:
            with self.assertRaises(HTTPError) as raised:
                post(message, **headers)
            self.assertEqual(raised.exception.code, code)
            raised.exception.close()
        self.assertEqual(len(read_messages(self.memory.directory)), 3)

    def test_cli_start_shutdown_and_exclusive_resume(self):
        repository = Path(__file__).resolve().parents[1]
        command = [sys.executable, '-m', 'smpa_ros.conversation_web',
                   '--config', str(repository / 'config.yaml'), '--port', '0',
                   '--world-model-root', self.temporary.name]
        process = subprocess.Popen(command, cwd=repository, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(ready, 'Server did not announce startup')
            directory = Path(process.stdout.readline().decode().strip().split('Conversation: ', 1)[1])
            url = process.stdout.readline().decode().strip().split('Open ', 1)[1]
            with urlopen(url + '/api/conversation', timeout=5) as response:
                self.assertEqual(json.load(response)['messages'], [])
            competing = subprocess.run(command + ['--resume', str(directory)], cwd=repository,
                                       capture_output=True, timeout=15)
            self.assertNotEqual(competing.returncode, 0)
            self.assertEqual(yaml.safe_load((directory / 'metadata.yaml').read_text())['status'], 'running')
            process.terminate()
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertEqual(yaml.safe_load((directory / 'metadata.yaml').read_text())['status'], 'interrupted')
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)

    def configured_agents(self):
        """Use the repository registry with isolated configuration and no credentials."""
        repository = Path(__file__).resolve().parents[1]
        config = yaml.safe_load((repository / 'config.yaml').read_text())
        path = Path(self.temporary.name) / 'config.yaml'
        path.write_text(yaml.safe_dump(config))
        with patch('dotenv.dotenv_values', return_value={}), patch.dict('os.environ', {}, clear=True):
            agents, default = discover_agents(config, path, Path(self.temporary.name) / 'models',
                                              repository / 'models')
        return agents, default, config, path

    def test_discovery_defaults_local_without_network_or_credentials(self):
        with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')), \
                patch('smpa_ros.backends.api.APIBackend') as api, \
                patch('smpa_ros.backends.transformers.TransformersBackend') as local:
            agents, default, config, path = self.configured_agents()
            self.assertEqual(agents[default]['agent']['model_id'], 'Qwen/Qwen3-4B')
            self.assertEqual(agents[default]['agent']['provider'], 'transformers')
            self.assertEqual(config['modeling']['backend'], 'api')
            self.assertEqual({item['agent']['source'] for item in agents.values()},
                             {'local:qwen3-0.6b', 'local:qwen3-1.7b', 'local:qwen3-4b',
                              'local:phi-4-mini-instruct', 'local:gemma-4-e4b', 'local:qwen3-8b',
                              'api:openai', 'api:nebula'})
            self.assertNotIn('reasoning_effort', agents[default]['agent']['generation'])
            self.assertIn('weights missing', agents[default]['availability'])
            for item in agents.values():
                if item['agent']['backend'] == 'api':
                    self.assertIn('Missing API credentials', item['availability'])
            with self.assertRaisesRegex(ValueError, 'weights missing'):
                select_agent(self.memory, agents, default)
            restore_agent(self.memory, agents, default)
            self.assertEqual(self.memory.metadata['active_agent']['id'], default)
            api.assert_not_called()
            local.assert_not_called()

    def test_incomplete_sharded_weights_cannot_be_selected(self):
        agents, default, config, config_path = self.configured_agents()
        path = Path(agents[default]['agent']['model_path'])
        path.mkdir(parents=True)
        for name in ('config.json', 'tokenizer.json'):
            (path / name).write_text('{}')
        (path / 'part1.safetensors').write_bytes(b'weights')
        (path / 'model.safetensors.index.json').write_text(json.dumps({
            'weight_map': {'a': 'part1.safetensors', 'b': 'part2.safetensors'}}))
        repository = Path(__file__).resolve().parents[1]
        agents, default = discover_agents(config, config_path, path.parents[1], repository / 'models')
        with self.assertRaisesRegex(ValueError, 'weights missing'):
            select_agent(self.memory, agents, default)
        (path / 'part2.safetensors').write_bytes(b'weights')
        agents, default = discover_agents(config, config_path, path.parents[1], repository / 'models')
        select_agent(self.memory, agents, default)
        self.assertEqual(self.memory.metadata['active_agent']['id'], default)

    def test_http_agent_switch_routes_backend_and_persists_provenance(self):
        agents, default, config, path = self.configured_agents()
        agents[default]['available'] = True  # Inference is mocked in this transport test.
        alternate = next(key for key, item in agents.items() if item['agent']['source'] == 'api:openai')
        restore_agent(self.memory, agents, default)
        server = make_server(self.memory, None, None, port=0, agents=agents)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        base = f'http://127.0.0.1:{server.server_port}'
        def post(route, data):
            with urlopen(Request(base + route, data=json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json'})) as response:
                return json.load(response)
        with patch('smpa_ros.backends.api.APIBackend') as api, \
                patch('smpa_ros.backends.transformers.TransformersBackend') as local:
            local.return_value.generate_conversation.return_value = {'response': 'Local reply'}
            api.return_value.generate_conversation.return_value = {'response': 'API reply'}
            with urlopen(base + '/api/agents') as response:
                self.assertEqual(json.load(response)['active_agent']['id'], default)
            post('/api/message', {'message': 'Remember a red cup', 'agent_id': default})
            api.assert_not_called()
            state = post('/api/agent', {'agent_id': alternate})
            self.assertEqual(len(state['messages']), 2)
            api.assert_not_called()  # Selection itself remains lazy.
            state = post('/api/message', {'message': 'What color?', 'agent_id': alternate})
            api.assert_called_once_with(path, modality='text', provider='openai')
            local.assert_called_once()
            self.assertEqual(api.return_value.generate_conversation.call_args.args[0], [
                {'role': 'user', 'content': 'Remember a red cup'},
                {'role': 'assistant', 'content': 'Local reply'},
                {'role': 'user', 'content': 'What color?'},
            ])
            self.assertEqual(state['messages'][1]['agent']['id'], default)
            self.assertEqual(state['messages'][3]['agent']['id'], alternate)
            root = self.memory.directory
            record = json.loads((root / 'state/cycle_000002/act.json').read_text())
            self.assertEqual(record['agent']['id'], alternate)
            self.assertEqual(record['agent']['generation'], state['generation']['values'])
            self.assertEqual(yaml.safe_load((root / 'metadata.yaml').read_text())['active_agent']['id'], alternate)
            self.assertIn('gpt-4.1-mini', (root / 'conversation.md').read_text())
            before = (root / 'conversation.md').read_bytes()
            for route, data in [('/api/agent', {'agent_id': 'invalid'}),
                                ('/api/message', {'message': 'No', 'agent_id': []})]:
                with self.assertRaises(HTTPError) as raised:
                    post(route, data)
                self.assertEqual(raised.exception.code, 400)
                raised.exception.close()
            self.assertEqual((root / 'conversation.md').read_bytes(), before)
            self.assertEqual(self.memory.metadata['active_agent']['id'], alternate)

    def test_saved_selection_and_legacy_runs_resume(self):
        agents, default, config, path = self.configured_agents()
        alternate = next(key for key, item in agents.items() if item['agent']['source'] == 'api:nebula')
        select_agent(self.memory, agents, alternate)
        exchange(self.memory, 'Hello', self.generate, {}, agent=agents[alternate]['agent'])
        # Older releases displayed only provider/model; labels are not identity.
        self.memory.metadata['active_agent']['label'] = 'API · nebula · SURF.gpt-oss-120b'
        self.memory.finalize('interrupted')
        self.memory = open_conversation(None, resume=self.memory.directory)
        restore_agent(self.memory, agents, default)
        self.assertEqual(self.memory.metadata['active_agent']['id'], alternate)
        self.assertEqual(read_messages(self.memory.directory)[1]['agent'],
                         dict(agents[alternate]['agent'], generation={}))
        # Legacy runs have only models.text and unannotated Markdown messages.
        legacy = open_conversation(Path(self.temporary.name), configuration=config,
                                   models={'text': agents[alternate]['agent']['model_configuration']})
        exchange(legacy, 'Legacy message', self.generate, {})
        legacy.finalize('completed')
        legacy = open_conversation(None, resume=legacy.directory)
        restore_agent(legacy, agents, default)
        self.assertEqual(legacy.metadata['active_agent']['id'], alternate)
        self.assertNotIn('agent', read_messages(legacy.directory)[1])
        legacy.finalize('completed')
        missing = dict(agents)
        del missing[alternate]
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            restore_agent(self.memory, missing, default)

    def test_configuration_identity_and_no_silent_cloud_fallback(self):
        agents, default, config, path = self.configured_agents()
        with patch('smpa_ros.backends.transformers.TransformersBackend', side_effect=OSError('Missing weights')), \
                patch('smpa_ros.backends.api.APIBackend') as api:
            restore_agent(self.memory, agents, default)
            with self.assertRaises(OSError):
                exchange(self.memory, 'Hello', agents[default]['generate'],
                         agents[default]['agent']['generation'], agent=agents[default]['agent'])
            api.assert_not_called()
            self.assertEqual(self.memory.metadata['active_agent']['id'], default)
            self.assertEqual(len(read_messages(self.memory.directory)), 1)
        config['modeling']['max_output_tokens'] = 64
        path.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(RuntimeError, 'Configuration changed'):
            agents[default]['generate']('Hello', max_new_tokens=8)
        repository = Path(__file__).resolve().parents[1]
        changed, new_default = discover_agents(config, path, Path(self.temporary.name) / 'models', repository / 'models')
        self.assertNotEqual(new_default, default)
        self.assertEqual(changed[new_default]['agent']['generation']['max_new_tokens'], 64)

    def test_api_explicit_reasoning_and_unchanged_plain_generation(self):
        from types import SimpleNamespace as N
        from smpa_ros.backends.api import APIBackend
        backend = APIBackend.__new__(APIBackend)
        backend.model_id = 'requested-model'
        backend._client = Mock(api_key='test-secret-key')
        message = N(content='Final answer', refusal=None)
        completion = N(model='actual-model', choices=[N(finish_reason='stop', message=message)], usage=None)
        backend._client.chat.completions.create.return_value = completion
        for field in ('reasoning_content', 'reasoning'):
            with self.subTest(field=field):
                setattr(message, field, 'Model supplied trace')
                result = backend.generate_conversation('Question')
                self.assertEqual(result, {'response': 'Final answer', 'thinking': 'Model supplied trace',
                                          'reasoning_source': field, 'model_id': 'actual-model',
                                          'effective_settings': {'max_new_tokens': 128, 'temperature': 0},
                                          'system_fingerprint': None})
                self.assertEqual(backend.generate('Question'), 'Final answer')
                delattr(message, field)
        # Counts and opaque objects are not traces and must not be converted to text.
        message.reasoning = {'encrypted_content': 'opaque'}
        self.assertNotIn('thinking', backend.generate_conversation('Question'))
        message.reasoning_content = 'test-secret-key'
        with self.assertRaisesRegex(RuntimeError, 'credential'):
            backend.generate_conversation('Question')
        message.reasoning_content = 'trace'
        message.content = ''
        with self.assertRaisesRegex(RuntimeError, 'empty text'):
            backend.generate_conversation('Question')

    def test_qwen_delimiters_are_model_specific_and_never_synthesized(self):
        from types import SimpleNamespace
        from smpa_ros.backends.transformers import TransformersBackend
        backend = TransformersBackend.__new__(TransformersBackend)
        backend.model = SimpleNamespace(config=SimpleNamespace(model_type='qwen3'))
        backend.generate = Mock(return_value='<think>Model trace</think>Final answer')
        self.assertEqual(backend.generate_conversation('Question'),
                         {'response': 'Final answer', 'thinking': 'Model trace', 'reasoning_source': 'qwen3:<think>'})
        for text in ('Final answer', '<think>\n\n</think>\nFinal answer'):
            backend.generate.return_value = text
            self.assertEqual(backend.generate_conversation('Question'), {'response': 'Final answer'})
        backend.generate.return_value = '<think>Incomplete trace'
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            backend.generate_conversation('Question')
        backend.model.config.model_type = 'other-model'
        literal = '<think>Quoted markup</think>Literal example'
        backend.generate.return_value = literal
        self.assertEqual(backend.generate_conversation('Question'), {'response': literal})

    def test_ollama_reasoning_field_and_plain_interface(self):
        from unittest.mock import MagicMock
        from smpa_ros.backends.ollama import OllamaBackend
        response = MagicMock()
        body = {'response': 'Final answer', 'thinking': 'Model trace', 'model': 'actual-local-model'}
        response.__enter__.return_value.read.side_effect = lambda: json.dumps(body).encode()
        with patch('smpa_ros.backends.ollama.urlopen', return_value=response):
            backend = OllamaBackend('requested-model')
            self.assertEqual(backend.generate_conversation('Question'),
                             {'response': 'Final answer', 'thinking': 'Model trace',
                              'reasoning_source': 'thinking', 'model_id': 'actual-local-model',
                              'effective_settings': {'max_new_tokens': 128, 'temperature': 0, 'seed': 0}})
            self.assertEqual(backend.generate('Question'), 'Final answer')
            del body['thinking']
            self.assertNotIn('thinking', backend.generate_conversation('Question'))

    def test_thinking_tags_roundtrip_provenance_and_history(self):
        agents, default, _, _ = self.configured_agents()
        agent = agents[default]['agent']
        thinking = 'Model trace ☕\nLiteral </thinking>\n<response> and ## Assistant'
        answer = 'Final answer\nLiteral </response>'
        self.generate.return_value = {'response': answer, 'thinking': thinking,
                                      'reasoning_source': 'provider-field', 'model_id': 'reported-model'}
        exchange(self.memory, 'Question', self.generate, {}, agent=agent)
        transcript = (self.memory.directory / 'conversation.md').read_text()
        self.assertIn('<thinking>\n' + thinking + '\n</thinking>\n\n<response>\n' + answer + '\n</response>', transcript)
        message = read_messages(self.memory.directory)[1]
        self.assertEqual(message['thinking'], thinking)
        self.assertEqual(message['response'], answer)
        self.assertEqual(message['content'], answer)
        self.assertEqual([part['type'] for part in message['parts']], ['thinking', 'response'])
        self.assertEqual(message['agent']['model_id'], 'reported-model')
        self.assertEqual(message['agent']['requested_model_id'], agent['model_id'])
        self.assertEqual(message['agent']['provider'], agent['provider'])
        record = json.loads((self.memory.directory / 'state/cycle_000001/act.json').read_text())
        self.assertEqual(record['thinking'], thinking)
        self.assertEqual(record['response'], answer)
        self.assertEqual(record['agent'], message['agent'])
        self.assertEqual(record['reasoning_source'], 'provider-field')
        self.memory.finalize('interrupted')
        self.memory = open_conversation(None, resume=self.memory.directory)
        self.assertEqual(read_messages(self.memory.directory)[1], message)
        self.generate.return_value = {'response': 'Another answer'}
        exchange(self.memory, 'Next question', self.generate, {}, agent=agent)
        prompt = self.generate.call_args.args[0]
        self.assertEqual(prompt[1], {'role': 'assistant', 'content': answer})
        self.assertNotIn('Model trace', json.dumps(prompt))
        self.assertNotIn('thinking', read_messages(self.memory.directory)[3])

    def test_legacy_literal_tags_and_response_only_do_not_create_traces(self):
        from smpa_ros.conversation import append_message
        literal = '<thinking>Only quoted text</thinking>\n<response>Example</response>'
        append_message(self.memory.directory, 'Assistant', literal)
        self.assertNotIn('thinking', read_messages(self.memory.directory)[0])
        self.assertEqual(read_messages(self.memory.directory)[0]['content'], literal)
        before = (self.memory.directory / 'conversation.md').read_bytes()
        for result in ('Plain response', {'response': 'Plain response'},
                       {'response': 'Plain response', 'thinking': None},
                       {'response': 'Plain response', 'thinking': '  '}):
            self.generate.return_value = result
            exchange(self.memory, 'Question', self.generate, {})
            self.assertNotIn('thinking', read_messages(self.memory.directory)[-1])
        transcript = (self.memory.directory / 'conversation.md').read_bytes()[len(before):]
        self.assertNotIn(b'<thinking>', transcript)
        self.assertEqual(transcript.count(b'<response>\nPlain response\n</response>'), 4)

    def test_http_returns_typed_reasoning_with_final_response(self):
        self.generate.return_value = {'response': 'Visible answer', 'thinking': 'Backend trace'}
        server = make_server(self.memory, self.generate, {}, port=0)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            request = Request(f'http://127.0.0.1:{server.server_port}/api/message',
                              data=b'{"message":"Question"}', headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                message = json.load(response)['messages'][-1]
            self.assertEqual(message['content'], 'Visible answer')
            self.assertEqual(message['thinking'], 'Backend trace')
            self.assertEqual(message['parts'], [{'type': 'thinking', 'text': 'Backend trace'},
                                               {'type': 'response', 'text': 'Visible answer'}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    @unittest.skipUnless(os.environ.get('SMPA_BROWSER_TEST') == '1' and shutil.which('firefox'),
                         'Set SMPA_BROWSER_TEST=1 with Firefox installed for browser verification')
    def test_browser_thinking_collapsed_expand_and_hide(self):
        """Exercise the production rendering script in a real optional browser."""
        from http.server import BaseHTTPRequestHandler, HTTPServer
        repository = Path(__file__).resolve().parents[1]
        page = (repository / 'smpa_ros/conversation.html').read_text()
        script = """<script>
        function reportCheck(value){
          const request=new XMLHttpRequest();request.open('POST','/check',false);
          request.send(JSON.stringify(value));
        }
        try{
          render(TEST_STATE);
          const details=document.querySelector('details.thinking');
          const answer=document.querySelector('[data-message-type="response"]');
          const closed=!details.open;
          details.querySelector('summary').click();const expanded=details.open;
          details.querySelector('summary').click();const collapsed=!details.open;
          const settingsClosed=!document.querySelector('#settings-panel').open;
          setBusy(true);
          const busyDisabled=[...document.querySelectorAll('#settings-fields input')].every(input=>input.disabled)
             &&document.querySelector('#agent').disabled&&document.querySelector('#new-run').disabled;
          setBusy(false);
          const temperatureEnabled=!document.querySelector('#setting-temperature').disabled;
          generation.values.do_sample=false;renderSettings();
          const greedyDisabled=document.querySelector('#setting-temperature').disabled&&document.querySelector('#setting-top_k').disabled;
          generation.unsupported.top_k='Unsupported by this provider';renderSettings();
          const unsupportedExplained=document.querySelector('#settings-fields').textContent.includes('Unsupported by this provider');
          reportCheck({settingsClosed,busyDisabled,temperatureEnabled,greedyDisabled,unsupportedExplained,
                       closed,expanded,collapsed,answer:answer.textContent,
                       thinking:details.querySelector('.trace').textContent});
        }catch(error){reportCheck({error:String(error)});}
        </script>"""
        self.generate.return_value = {'response': 'Visible answer', 'thinking': 'Backend trace'}
        exchange(self.memory, 'Question', self.generate, {})
        state = {'active_agent': None, 'agents': [], 'graph': {'nodes': [], 'edges': []},
                 'messages': read_messages(self.memory.directory)}
        state['generation'] = {'values': {'max_new_tokens': 512, 'temperature': 0.7, 'do_sample': True,
                                          'seed': 42, 'top_p': 0.9, 'top_k': 20, 'repetition_penalty': 1.0,
                                          'enable_thinking': False},
                               'overrides': {}, 'origins': {}, 'unsupported': {}}
        # Finish the assertion before screenshot mode exits; asynchronous fetch
        # can otherwise race Firefox's page-load screenshot completion.
        page = page.replace('</html>', script.replace('TEST_STATE', json.dumps(state)) + '</html>').encode()
        report = {}
        completed = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = page if self.path == '/' else json.dumps(state).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/json')
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                report.update(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                completed.set()
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            # A non-hidden checkout-local profile also works with Ubuntu's Firefox snap.
            # Screenshot mode exits itself; wait before removing its temporary profile.
            with tempfile.TemporaryDirectory(prefix='browser-check-', dir=repository) as folder:
                result = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', folder,
                                         '--screenshot', str(Path(folder) / 'check.png'),
                                         f'http://127.0.0.1:{server.server_port}/'],
                                        capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertTrue(completed.wait(2), 'No browser rendering report received')
                self.assertEqual(report, {'settingsClosed': True, 'busyDisabled': True, 'temperatureEnabled': True,
                                          'greedyDisabled': True, 'unsupportedExplained': True,
                                          'closed': True, 'expanded': True, 'collapsed': True,
                                          'answer': 'Visible answer', 'thinking': 'Backend trace'})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_four_turn_history_reaches_qwen_template_without_flattening(self):
        from unittest.mock import MagicMock
        from smpa_ros.backends.transformers import TransformersBackend
        backend = TransformersBackend.__new__(TransformersBackend)
        backend.enable_thinking = False
        backend.torch = MagicMock()
        backend.model = MagicMock()
        backend.model.config.model_type = 'qwen3'
        backend.tokenizer = MagicMock()
        backend.tokenizer.chat_template = 'Qwen template'
        backend.tokenizer.apply_chat_template.return_value = 'formatted turns'
        backend.tokenizer.return_value.to.return_value['input_ids'].shape = (1, 5)
        answers = ['Hello!', 'I am a language model.', 'A local Qwen model.', 'Let us continue.']
        backend.tokenizer.decode.side_effect = answers
        expected = []
        for user, answer in zip(('Hello', 'Who are you?', 'No, I mean WHO are you?',
                                 'This conversation seems stuck.'), answers):
            expected.append({'role': 'user', 'content': user})
            exchange(self.memory, user, backend.generate_conversation, {'max_new_tokens': 4096})
            backend.tokenizer.apply_chat_template.assert_called_with(
                expected, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            expected.append({'role': 'assistant', 'content': answer})
        self.assertEqual(backend.model.generate.call_count, 4)
        for call in backend.model.generate.call_args_list:
            self.assertEqual(call.kwargs['max_new_tokens'], 4096)
            self.assertIs(call.kwargs['do_sample'], False)
        self.assertEqual([item['content'] for item in read_messages(self.memory.directory)[1::2]], answers)

    def test_api_and_ollama_conversations_preserve_role_messages(self):
        from types import SimpleNamespace as N
        from unittest.mock import MagicMock
        from smpa_ros.backends.api import APIBackend
        from smpa_ros.backends.ollama import OllamaBackend
        history = [{'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Hello!'},
                   {'role': 'user', 'content': 'Who are you?'}]
        api = APIBackend.__new__(APIBackend)
        api.model_id = 'test-model'
        api._client = Mock(api_key='test-key')
        api._client.chat.completions.create.return_value = N(
            choices=[N(finish_reason='stop', message=N(content='An assistant', refusal=None))], usage=None)
        self.assertEqual(api.generate_conversation(history)['response'], 'An assistant')
        self.assertEqual(api._client.chat.completions.create.call_args.kwargs['messages'], history)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'model': 'test-model', 'message': {'content': 'An assistant', 'thinking': 'Trace'}}).encode()
        with patch('smpa_ros.backends.ollama.urlopen', return_value=response) as request:
            result = OllamaBackend('test-model').generate_conversation(history)
            self.assertEqual(result['response'], 'An assistant')
            self.assertEqual(result['thinking'], 'Trace')
            self.assertEqual(request.call_args.args[0].full_url, 'http://localhost:11434/api/chat')
            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload['messages'], history)
            self.assertNotIn('prompt', payload)

    @unittest.skipUnless(os.environ.get('SMPA_QWEN_TEST') == '1',
                         'Set SMPA_QWEN_TEST=1 to compare actual local Qwen and GUI inference')
    def test_qwen_four_turn_http_matches_direct(self):
        """Compare real GUI inference, direct chat, and the former JSON wrapper.

        Only existing local weights are used. The separate debug report captures
        synthetic prompts and raw continuations; transcript framing is unchanged.
        """
        import logging
        import torch
        from smpa_ros.backends.transformers import TransformersBackend
        repository = Path(__file__).resolve().parents[1]
        checkpoint = repository / 'models/llm/qwen3_0.6b'
        if not (checkpoint / 'model.safetensors').is_file():
            self.skipTest('Existing Qwen3-0.6B weights are unavailable; no download attempted')
        previous_threads = torch.get_num_threads()
        torch.set_num_threads(2)
        self.addCleanup(torch.set_num_threads, previous_threads)
        backend = TransformersBackend(str(checkpoint), enable_thinking=False)
        config_path = repository / 'config.yaml'
        config = yaml.safe_load(config_path.read_text())
        agents, default = discover_agents(config, config_path, repository / 'models', repository / 'models')
        default = next(key for key, item in agents.items() if item['agent']['source'] == 'local:qwen3-0.6b')
        restore_agent(self.memory, agents, default)
        from smpa_ros.conversation_web import update_generation
        update_generation(self.memory, agents, {'do_sample': False, 'max_new_tokens': 4096})
        settings = agents[default]['agent']['generation']
        report = {'settings': settings, 'effective_do_sample': False,
                  'generation_defaults': backend.model.generation_config.to_dict(), 'turns': []}
        if os.environ.get('SMPA_QWEN_LOG'):
            log_path = Path(os.environ['SMPA_QWEN_LOG'])
        else:
            with tempfile.NamedTemporaryFile(prefix='smpa-qwen-', suffix='.json', delete=False) as output:
                log_path = Path(output.name)
        server = make_server(self.memory, None, None, port=0, agents=agents)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        logger = 'smpa_ros.backends.transformers'
        legacy_history, replies = [], []
        try:
            with patch('smpa_ros.backends.transformers.TransformersBackend', return_value=backend), \
                    patch('smpa_ros.backends.api.APIBackend', side_effect=AssertionError('Unexpected API inference')):
                for user in ('Hello', 'Who are you?', 'No, I mean WHO are you?', 'This conversation seems stuck.'):
                    # The real HTTP route loads history from the persisted transcript.
                    with self.assertLogs(logger, level=logging.DEBUG) as logs:
                        request = Request(f'http://127.0.0.1:{server.server_port}/api/message',
                                          data=json.dumps({'message': user, 'agent_id': default}).encode(),
                                          headers={'Content-Type': 'application/json'})
                        with urlopen(request, timeout=300) as response:
                            state = json.load(response)
                    gui_log = json.loads(logs.records[-1].getMessage())
                    gui_answer = state['messages'][-1]['response']
                    history = [{key: message[key] for key in ('role', 'content')}
                               for message in read_messages(self.memory.directory)[:-1]]
                    # Direct inference bypasses exchange(), the HTTP route, and
                    # generate_conversation(), but uses identical weights/settings.
                    formatted = backend.tokenizer.apply_chat_template(
                        history, tokenize=False, add_generation_prompt=True, enable_thinking=False)
                    inputs = backend.tokenizer(formatted, return_tensors='pt', add_special_tokens=False).to(backend.model.device)
                    with torch.no_grad():
                        output = backend.model.generate(**inputs, **settings, do_sample=False)
                    tokens = output[0, inputs['input_ids'].shape[-1]:]
                    direct = backend.tokenizer.decode(tokens, skip_special_tokens=True)
                    raw = backend.tokenizer.decode(tokens, skip_special_tokens=False)
                    # Retain the old input construction only as a diagnostic control.
                    legacy_history.append({'role': 'user', 'content': user})
                    legacy_prompt = ('Continue this conversation. Respond to the latest user message as the assistant. '
                                     'Conversation history follows as JSON; its role labels identify the speakers.\n\n'
                                     + json.dumps(legacy_history, ensure_ascii=False))
                    with self.assertLogs(logger, level=logging.DEBUG) as old_logs:
                        legacy = backend.generate_conversation(legacy_prompt, **settings)['response']
                    legacy_history.append({'role': 'assistant', 'content': legacy})
                    report['turns'].append({'user': user, 'history': history,
                        'gui': dict(gui_log, response=gui_answer),
                        'direct': {'formatted_prompt': formatted, 'raw_output': raw, 'response': direct},
                        'legacy_wrapper': dict(json.loads(old_logs.records[-1].getMessage()), response=legacy)})
                    log_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
                    self.assertEqual(gui_log['formatted_prompt'], formatted)
                    self.assertEqual(gui_log['raw_output'], raw)
                    self.assertEqual(gui_answer, direct)
                    self.assertEqual(history[-1], {'role': 'user', 'content': user})
                    replies.append(gui_answer)
                self.assertNotEqual(replies[1], "I'm here to help! What can I do for you?")
                self.assertGreater(len(set(replies)), 1)
                # Seeded sampling uses the same loaded weights and restores CPU RNG state.
                sampling = dict(max_new_tokens=64, do_sample=True, temperature=0.7, top_p=0.9,
                                top_k=20, repetition_penalty=1.0, seed=42, enable_thinking=False)
                history = [{'role': 'user', 'content': 'Say a short greeting.'}]
                rng_before = torch.random.get_rng_state()
                first = backend.generate_conversation(history, **sampling)
                second = backend.generate_conversation(history, **sampling)
                self.assertEqual(first['response'], second['response'])
                self.assertTrue(torch.equal(rng_before, torch.random.get_rng_state()))
                thought = backend.generate_conversation(
                    [{'role': 'user', 'content': 'What is 1+1? Answer briefly.'}],
                    max_new_tokens=1024, do_sample=False, enable_thinking=True, seed=42)
                self.assertTrue(thought.get('thinking'))
                self.assertTrue(thought['response'])
                self.assertNotIn('</think>', thought['response'])
                report['sampling'] = first
                report['thinking_check'] = thought
                log_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            print(f'Qwen reproduction debug report: {log_path}', flush=True)

    def test_generation_controls_and_run_lifecycle(self):
        import fcntl
        agents, default, config, config_path = self.configured_agents()
        agents[default]['available'] = True
        local = Mock(side_effect=lambda prompt, **kwargs: {'response': 'Answer', 'effective_settings': kwargs})
        agents[default]['generate'] = local
        restore_agent(self.memory, agents, default)
        server = make_server(self.memory, None, None, port=0, agents=agents,
                             root=Path(self.temporary.name) / 'other-root')
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(route, data):
            with urlopen(Request(base + route, data=json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json'})) as response:
                return json.load(response)
        try:
            original = self.memory.directory
            original_id = self.memory.metadata['run_id']
            state = post('/api/settings', {'overrides': {'temperature': 0.2, 'seed': 7,
                         'enable_thinking': True, 'max_new_tokens': 64}})
            self.assertEqual(state['generation']['values']['seed'], 7)
            state = post('/api/message', {'message': 'Hello'})
            self.assertEqual(local.call_args.kwargs['temperature'], 0.2)
            self.assertEqual(local.call_args.kwargs['seed'], 7)
            self.assertTrue(local.call_args.kwargs['enable_thinking'])
            provenance = state['messages'][-1]['agent']
            self.assertEqual(provenance['generation']['temperature'], 0.2)
            self.assertEqual(provenance['generation_overrides']['seed'], 7)
            self.assertIn('revision', provenance['model_configuration'])
            post('/api/settings', {'overrides': {'temperature': 0.9, 'seed': 12}})
            state = post('/api/message', {'message': 'Another question'})
            self.assertEqual(state['messages'][1]['agent']['generation']['temperature'], 0.2)
            self.assertEqual(state['messages'][3]['agent']['generation']['temperature'], 0.9)
            self.assertEqual(len(local.call_args.args[0]), 3)
            before = (original / 'conversation.md').read_bytes()
            # Invalid values are rejected without inference or saved messages.
            for values in ({'temperature': float('nan')}, {'seed': True}, {'max_new_tokens': 0},
                           {'top_p': 2}, {'do_sample': 'false'}, {'temperature': 0, 'do_sample': True}):
                with self.assertRaises(HTTPError) as raised:
                    post('/api/settings', {'overrides': values})
                self.assertEqual(raised.exception.code, 400)
                raised.exception.close()
            state = post('/api/clear', {})
            self.assertEqual(state['messages'], [])
            self.assertNotEqual(state['run_id'], original_id)
            self.assertEqual(state['generation']['values']['seed'], 12)
            self.assertEqual((original / 'conversation.md').read_bytes(), before)
            api = next(k for k, item in agents.items() if item['agent']['source'] == 'api:openai')
            state = post('/api/agent', {'agent_id': api})
            self.assertIn('top_k', state['generation']['unsupported'])
            with self.assertRaises(HTTPError) as raised:
                post('/api/settings', {'overrides': {'top_k': 10}})
            self.assertIn('Unsupported', json.load(raised.exception)['error'])
            state = post('/api/agent', {'agent_id': default})
            self.assertEqual(state['generation']['values']['seed'], 12)
            state = post('/api/settings', {'overrides': {}})
            self.assertEqual(state['generation']['values'], agents[default]['defaults'])
            state = post('/api/new', {})
            self.assertEqual(state['messages'], [])
            self.assertEqual(state['generation']['overrides'], {})
            state = post('/api/resume', {'run_id': original_id})
            self.assertEqual(len(state['messages']), 4)
            self.assertEqual(state['generation']['values']['seed'], 12)
            self.assertEqual((original / 'conversation.md').read_bytes(), before)
            with urlopen(base + '/api/conversation') as response:
                self.assertEqual(json.load(response)['generation'], state['generation'])
            with (original / 'conversation.md').open('rb') as competing:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(HTTPError) as raised:
                post('/api/resume', {'run_id': '../outside'})
            raised.exception.close()
            self.assertEqual(server.memory.metadata['run_id'], original_id)
        finally:
            server.shutdown(); server.server_close(); thread.join()
            server.memory.finalize('completed')
        resumed = open_conversation(None, resume=original)
        restore_agent(resumed, agents, default)
        self.assertEqual(resumed.metadata['generation_settings'][default]['overrides']['seed'], 12)
        resumed.finalize('completed')

    def test_transformers_generation_controls_and_unchanged_smpa(self):
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from smpa_ros.backends.transformers import TransformersBackend
        backend = TransformersBackend.__new__(TransformersBackend)
        backend.enable_thinking = False
        backend.torch = MagicMock()
        backend.torch.cuda.device_count.return_value = 0
        backend.model = MagicMock()
        backend.model.config.model_type = 'qwen3'
        backend.model.generation_config.to_dict.return_value = {'eos_token_id': 42, 'temperature': 1.0}
        backend.model.device = SimpleNamespace(type='cpu')
        backend.tokenizer = MagicMock()
        backend.tokenizer.chat_template = 'Qwen template'
        backend.tokenizer.return_value.to.return_value['input_ids'].shape = (1, 5)
        backend.tokenizer.decode.return_value = 'Actual trace</think>\nFinal answer'
        result = backend.generate_conversation([{'role': 'user', 'content': 'Hello'}],
            max_new_tokens=64, enable_thinking=True, do_sample=True, temperature=0.3,
            top_p=0.8, top_k=12, repetition_penalty=1.2, seed=123)
        self.assertEqual(result['thinking'], 'Actual trace')
        self.assertEqual(result['response'], 'Final answer')
        backend.tokenizer.apply_chat_template.assert_called_with(
            [{'role': 'user', 'content': 'Hello'}], tokenize=False, add_generation_prompt=True, enable_thinking=True)
        options = backend.model.generate.call_args.kwargs
        for name, value in dict(temperature=0.3, top_p=0.8, top_k=12,
                                repetition_penalty=1.2, do_sample=True).items():
            self.assertEqual(options[name], value)
        backend.torch.manual_seed.assert_called_with(123)
        self.assertEqual(result['effective_settings']['seed'], 123)
        self.assertEqual(result['effective_settings']['eos_token_id'], 42)
        backend.tokenizer.decode.return_value = 'Plain answer'
        result = backend.generate_conversation('Question', do_sample=False, temperature=0.8,
                                               top_p=0.9, top_k=20, enable_thinking=False, seed=9)
        options = backend.model.generate.call_args.kwargs
        self.assertFalse(options['do_sample'])
        for key in ('temperature', 'top_p', 'top_k'):
            self.assertNotIn(key, options)
            self.assertIn(key, result['effective_settings']['inactive'])
        backend.generate('SMPA prompt')
        self.assertEqual(backend.model.generate.call_args.kwargs, {'max_new_tokens': 128, 'do_sample': False})
        self.assertFalse(backend.tokenizer.apply_chat_template.call_args.kwargs['enable_thinking'])
        backend.model.config.model_type = 'phi3'
        with self.assertRaisesRegex(ValueError, 'enable_thinking'):
            backend.generate_conversation('Question', enable_thinking=True)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            backend.generate_conversation('Question', unknown=4)

    def test_api_controls_are_forwarded_and_unsupported_controls_rejected(self):
        from types import SimpleNamespace as N
        from smpa_ros.backends.api import APIBackend
        backend = APIBackend.__new__(APIBackend)
        backend.model_id = 'configured-model'
        backend._client = Mock(api_key='test-secret')
        backend._client.chat.completions.create.return_value = N(
            model='actual-model', system_fingerprint='backend-version', usage=None,
            choices=[N(finish_reason='stop', message=N(content='Answer', refusal=None))])
        result = backend.generate_conversation('Question', max_new_tokens=64, temperature=0.3, top_p=0.8, seed=42)
        request = backend._client.chat.completions.create.call_args.kwargs
        self.assertEqual(request['seed'], 42)
        self.assertEqual(request['temperature'], 0.3)
        self.assertEqual(request['top_p'], 0.8)
        self.assertEqual(result['system_fingerprint'], 'backend-version')
        self.assertEqual(result['effective_settings']['seed'], 42)
        for values in ({'top_k': 20}, {'do_sample': False}, {'enable_thinking': True}, {'repetition_penalty': 1.2}):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                backend.generate_conversation('Question', **values)
        backend.generate('SMPA prompt')
        request = backend._client.chat.completions.create.call_args.kwargs
        self.assertEqual(request['temperature'], 0)
        self.assertNotIn('seed', request)
        self.assertNotIn('top_p', request)

    def test_generation_edits_reuse_lazy_model_and_do_not_extend_robot_controls(self):
        from smpa_ros.model import load_backend
        agents, default, config, config_path = self.configured_agents()
        repository = Path(__file__).resolve().parents[1]
        config['modeling'] = {'model': 'qwen3-0.6b', 'backend': 'transformers'}
        with patch('smpa_ros.backends.transformers.TransformersBackend') as local:
            generate, _ = load_backend(config, config_path, repository / 'models', repository / 'models', conversation=True)
            generate('First', max_new_tokens=32, temperature=0.2, seed=1)
            generate('Second', max_new_tokens=64, temperature=0.8, seed=2)
            local.assert_called_once()
            self.assertEqual(local.return_value.generate_conversation.call_args.kwargs['temperature'], 0.8)
            robot, _ = load_backend(config, config_path, repository / 'models', repository / 'models')
            with self.assertRaisesRegex(ValueError, 'only in conversations'):
                robot('Robot', max_new_tokens=32, temperature=0.5)

    def test_resume_rejects_robot_run(self):
        from memory import MemoryManager
        robot = MemoryManager(Path(self.temporary.name), goal='# Robot goal')
        with self.assertRaisesRegex(ValueError, 'Only conversational'):
            MemoryManager.resume_conversation(robot.directory)
        robot.finalize('completed')


if __name__ == '__main__':
    unittest.main()
