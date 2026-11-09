"""Local browser transport for one conversational run; no ROS initialization.

A single HTTP server serializes exchanges and owns the transcript lock. The
existing lazy text-backend loader supplies inference without Model or Plan calls.
"""
import argparse
from copy import deepcopy
import hashlib
import os
import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
import json
from pathlib import Path
import signal

import yaml

from smpa_ros.conversation import exchange, open_conversation, read_messages
from smpa_ros.model import load_backend, validate_settings


def discover_agents(config, config_path, model_root, registry):
    """Derive inference configurations from existing providers and text models.

    The GUI defaults to the repository's lightweight Qwen entry independently of
    the robot pipeline. API-only reasoning controls do not apply to local agents.
    No model is loaded, downloaded, or contacted during discovery.
    """
    config = deepcopy(config)
    entries = yaml.safe_load((registry / 'model_library.yaml').read_text())['models']
    choices = [(f'local:{name}', dict(backend=item['backend'], model=name))
               for name, item in entries.items() if item.get('backend') in ('transformers', 'ollama')]
    choices += [(f'api:{name}', dict(backend='api', provider=name))
                for name, item in config.items() if isinstance(item, dict)
                and isinstance(item.get('models'), dict) and item['models'].get('text')
                and item.get('base_url') and item.get('api_key_env')]
    agents = {}
    default = None
    for source, selection in choices:
        pipeline = deepcopy(config)
        pipeline['modeling'] = dict(config.get('modeling', {}), **selection)
        if selection['backend'] != 'api':
            pipeline['modeling'].pop('reasoning_effort', None)
        settings = dict(config.get('stage_settings', {}).get('model', {'max_new_tokens': 512}))
        if 'max_output_tokens' in pipeline['modeling']:
            settings['max_new_tokens'] = pipeline['modeling']['max_output_tokens']
        if pipeline['modeling'].get('reasoning_effort') is not None:
            settings['reasoning_effort'] = pipeline['modeling']['reasoning_effort']
        if selection['backend'] != 'api':
            settings.pop('reasoning_effort', None)
        generate, selected = load_backend(pipeline, config_path, model_root, registry, conversation=True)
        agent = dict(source=source, backend=selected['backend'],
                     provider=selected.get('provider', selected['backend']),
                     model_id=selected['model_id'], generation=settings,
                     model_configuration=selected)
        if selected['backend'] == 'api':
            provider = config[selected['provider']]
            agent['endpoint'] = provider['base_url']
            agent['timeout'] = provider.get('timeout', 180)
        elif selected['backend'] == 'transformers':
            agent['model_path'] = str((Path(model_root) / selected['path']).resolve())
        # Identity covers configuration, not only the displayed model name.
        agent['id'] = source + ':' + hashlib.sha256(json.dumps(agent, sort_keys=True).encode()).hexdigest()[:16]
        label = 'API' if selected['backend'] == 'api' else 'Local'
        agent['label'] = f"{label} · {agent['provider']} · {agent['model_id']}"
        availability = 'Loaded on first message; availability not yet verified.'
        if selected['backend'] == 'transformers':
            path = Path(agent['model_path'])
            if not (path / 'config.json').is_file() or not any(path.glob('*.safetensors')):
                availability = 'Local weights missing. Install the selected model before sending; no cloud fallback.'
        elif selected['backend'] == 'api':
            from dotenv import dotenv_values
            env_path = Path(config_path).resolve().parent / '.env'
            if not env_path.is_file():
                env_path = Path.cwd() / '.env'
            key = provider['api_key_env']
            value = os.environ.get(key) if key in os.environ else dotenv_values(env_path).get(key)
            if not value or not value.strip():
                availability = f'Missing API credentials: set {key} in the environment or .env.'
        def checked_generate(prompt, *, _generate=generate, **options):
            # APIBackend reads this file lazily. Reject edits rather than record
            # an old model identity for a newly edited provider configuration.
            if yaml.safe_load(Path(config_path).read_text()) != config:
                raise RuntimeError('Configuration changed; restart with the original configuration')
            return _generate(prompt, **options)
        agents[agent['id']] = dict(agent=agent, generate=checked_generate, availability=availability)
        if source == 'local:qwen3-0.6b':
            default = agent['id']
    if default is None:
        raise ValueError('The conversational default requires registry entry qwen3-0.6b; no API fallback')
    return agents, default


def select_agent(memory, agents, identifier):
    """Persist an explicit selection without touching history or loading weights."""
    if not isinstance(identifier, str) or identifier not in agents:
        raise ValueError('Unknown agent configuration; select an agent from the list')
    memory.metadata['active_agent'] = deepcopy(agents[identifier]['agent'])
    memory.write_metadata()


def restore_agent(memory, agents, default):
    """Restore saved selection; older runs retain their recorded text provider."""
    saved = memory.metadata.get('active_agent')
    if saved:
        identifier = saved['id']
        if identifier not in agents or agents[identifier]['agent'] != saved:
            raise ValueError('Saved agent configuration is unavailable; restore its original configuration')
    else:
        prior = memory.metadata.get('models', {}).get('text')
        identifier = default
        if prior:
            matches = [key for key, value in agents.items()
                       if all(value['agent']['model_configuration'].get(field) == prior.get(field)
                              for field in ('backend', 'model_id', 'provider'))]
            if not matches:
                raise ValueError('Previous conversation model is unavailable; restore its configuration')
            identifier = matches[0]
    select_agent(memory, agents, identifier)


def make_server(memory, generate, settings, port=8000, *, agents=None):
    """Serialize selection and inference requests on the loopback interface."""
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
                        graph={'nodes': [], 'edges': []},
                        active_agent=memory.metadata.get('active_agent'),
                        agents=[dict(item['agent'], availability=item['availability'])
                                for item in (agents or {}).values()])

        def do_GET(self):
            if self.path == '/':
                self.reply(200, files('smpa_ros').joinpath('conversation.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path in ('/api/conversation', '/api/agents'):
                self.reply(200, self.state())
            else:
                self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if self.path not in ('/api/message', '/api/agent'):
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
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object')
                if self.path == '/api/message' and (not isinstance(data.get('message'), str) or not data['message'].strip()):
                    raise ValueError('Message must be nonempty text')
                if self.path == '/api/agent' or 'agent_id' in data:
                    select_agent(memory, agents or {}, data.get('agent_id'))
            except (ValueError, UnicodeError) as error:
                self.reply(400, {'error': str(error)})
                return
            if self.path == '/api/agent':
                self.reply(200, self.state())
                return
            try:
                if agents:
                    selected = agents[memory.metadata['active_agent']['id']]
                    exchange(memory, data['message'], selected['generate'], selected['agent']['generation'],
                             agent=selected['agent'])
                else:
                    exchange(memory, data['message'], generate, settings)
            except Exception:
                # Detailed backend errors live in the run record, not browser output.
                self.reply(500, dict(self.state(), error='Response failed. Your message is saved. Check the selected agent’s credentials, local weights or service, and the run record. No fallback was used.'))
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
    agents, default = discover_agents(config, options.config, options.model_root, registry)
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
                                   configuration=config)
        restore_agent(memory, agents, default)
        if lock is None:
            lock = (memory.directory / 'conversation.md').open('rb')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        server = make_server(memory, None, None, options.port, agents=agents)
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
