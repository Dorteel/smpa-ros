"""Conversation tests run with unittest without ROS, or in the repository suite."""
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import select
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

import yaml

from smpa_ros.conversation import exchange, open_conversation, read_messages
from smpa_ros.conversation_web import make_server


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
        prompt = self.generate.call_args.args[0]
        self.assertIn('There is a red cup.', prompt)
        self.assertIn('The cup is red.', prompt)
        self.assertIn('What color is it?', prompt)
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
        self.assertIn('Remember this.', self.generate.call_args.args[0])
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

    def test_resume_rejects_robot_run(self):
        from memory import MemoryManager
        robot = MemoryManager(Path(self.temporary.name), goal='# Robot goal')
        with self.assertRaisesRegex(ValueError, 'Only conversational'):
            MemoryManager.resume_conversation(robot.directory)
        robot.finalize('completed')


if __name__ == '__main__':
    unittest.main()
