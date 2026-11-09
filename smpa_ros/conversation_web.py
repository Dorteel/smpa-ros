"""Local browser transport for one conversational run; no ROS initialization.

A single HTTP server serializes exchanges and owns the transcript lock. The
existing lazy text-backend loader supplies inference without Model or Plan calls.
"""
import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
import json
from pathlib import Path
import signal

import yaml

from smpa_ros.conversation import exchange, open_conversation, read_messages
from smpa_ros.model import load_backend, validate_settings


def make_server(memory, generate, settings, port=8000):
    """Serve only the GUI and its two JSON routes on the loopback interface."""
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, payload, content_type='application/json'):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def state(self):
            return dict(messages=read_messages(memory.directory), run_id=memory.metadata['run_id'],
                        graph={'nodes': [], 'edges': []})

        def do_GET(self):
            if self.path == '/':
                self.reply(200, files('smpa_ros').joinpath('conversation.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path == '/api/conversation':
                self.reply(200, self.state())
            else:
                self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if self.path != '/api/message':
                self.reply(404, {'error': 'Not found'})
                return
            # Reject cross-origin browser writes and non-JSON form submissions.
            origin = self.headers.get('Origin')
            if (origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',
                                          f'http://localhost:{self.server.server_port}')):
                self.reply(403, {'error': 'Cross-origin requests are not allowed'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Send a JSON message of at most 64 KiB')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or not isinstance(data.get('message'), str) or not data['message'].strip():
                    raise ValueError('Message must be nonempty text')
            except (ValueError, UnicodeError) as error:
                self.reply(400, {'error': str(error)})
                return
            try:
                exchange(memory, data['message'], generate, settings)
            except Exception:
                # Detailed backend errors live in the run record, not browser output.
                self.reply(500, dict(self.state(), error='Response failed. Your message is saved; check the run record.'))
            else:
                self.reply(200, self.state())

        def log_message(self, format, *args):
            pass

    return HTTPServer(('127.0.0.1', port), Handler)


def main(args=None):
    """Load existing configuration, lock one run, and finalize it on shutdown."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.cwd() / 'config.yaml')
    parser.add_argument('--model-root', type=Path, default=Path.cwd() / 'models')
    parser.add_argument('--world-model-root', type=Path, default=Path.cwd() / 'memory')
    parser.add_argument('--resume', type=Path, help='Existing conversational run directory')
    parser.add_argument('--port', type=int, default=8000)
    options = parser.parse_args(args)
    config = yaml.safe_load(options.config.read_text())
    # Resumed runs retain the original pipeline, so model provenance stays accurate.
    if options.resume:
        config = yaml.safe_load((options.resume / 'metadata.yaml').read_text())['configuration']['pipeline']
        current = yaml.safe_load(options.config.read_text())
        if config != current:
            parser.error('Resume requires the original pipeline configuration')
    validate_settings(config.get('modeling', {}))
    registry = options.config.resolve().parent / 'models'
    if not (registry / 'model_library.yaml').is_file():
        from ament_index_python.packages import get_package_share_directory
        registry = Path(get_package_share_directory('smpa_ros')) / 'models'
    generate, selected = load_backend(config, options.config, options.model_root, registry)
    settings = dict(config.get('stage_settings', {}).get('model', {'max_new_tokens': 512}))
    modeling = config.get('modeling', {})
    if 'max_output_tokens' in modeling:
        settings['max_new_tokens'] = modeling['max_output_tokens']
    if modeling.get('reasoning_effort') is not None:
        settings['reasoning_effort'] = modeling['reasoning_effort']
    lock = None
    memory = None
    server = None
    status = 'completed'
    previous = signal.getsignal(signal.SIGTERM)
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        # Acquire the existing transcript before changing resumed metadata.
        if options.resume:
            lock = (options.resume / 'conversation.md').open('rb')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        memory = open_conversation(options.world_model_root, resume=options.resume,
                                   configuration=config, models={'text': selected})
        if lock is None:
            lock = (memory.directory / 'conversation.md').open('rb')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        server = make_server(memory, generate, settings, options.port)
        print(f'Conversation: {memory.directory}\nOpen http://127.0.0.1:{server.server_port}', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        status = 'interrupted'
    except BaseException:
        status = 'failed'
        raise
    finally:
        if server is not None:
            server.server_close()
        if memory is not None:
            memory.finalize(status)
        if lock is not None:
            lock.close()
        signal.signal(signal.SIGTERM, previous)
        # The dedicated process exits here, releasing the lazy backend and model.


if __name__ == '__main__':
    main()
