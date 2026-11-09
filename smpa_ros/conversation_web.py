"""Local browser transport for one conversational run; no ROS initialization.

A single HTTP server serializes exchanges and owns the transcript lock. The
existing lazy text-backend loader supplies conversation and belief inference;
physical robot stages are not invoked.
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

from smpa_ros.conversation import (exchange, open_conversation, read_messages,
                                   GENERATION_FIELDS, validate_generation)
from smpa_ros.belief import graph_view
from smpa_ros.model import load_backend, validate_settings


def discover_agents(config, config_path, model_root, registry):
    """Derive inference configurations from existing providers and text models.

    The GUI defaults to the repository's Qwen3-4B entry independently of
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
        agent['label'] = f"{label} · {source.split(':', 1)[1]} · {agent['provider']} · {agent['model_id']}"
        available = True
        availability = 'Loaded on first message; availability not yet verified.'
        if selected['backend'] == 'transformers':
            path = Path(agent['model_path'])
            weights = list(path.glob('*.safetensors'))
            index = path / 'model.safetensors.index.json'
            if index.is_file():
                try:
                    weights = [path / name for name in set(json.loads(index.read_text())['weight_map'].values())]
                except (ValueError, KeyError, TypeError):
                    weights = []
            available = ((path / 'config.json').is_file()
                         and any((path / name).is_file() for name in ('tokenizer.json', 'tokenizer.model'))
                         and bool(weights) and all(file.is_file() and file.stat().st_size > 0 for file in weights))
            if not available:
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
        # Controls are outside the immutable agent identity, so old runs and the
        # lazy model instance survive settings edits. Read only small local JSON.
        defaults = {'max_new_tokens': 512}
        origins = {'max_new_tokens': 'Conversation default'}
        supported = {'max_new_tokens'}
        if selected['backend'] == 'transformers':
            supported = GENERATION_FIELDS - {'enable_thinking'}
            defaults.update(temperature=0.7, top_p=0.9, top_k=20,
                            repetition_penalty=1.0, do_sample=True, seed=42)
            origins.update({name: 'Conversation default' for name in defaults if name not in origins})
            checkpoint = Path(agent['model_path']) / 'generation_config.json'
            if checkpoint.is_file():
                checkpoint_defaults = json.loads(checkpoint.read_text())
                for name in ('temperature', 'top_p', 'top_k', 'repetition_penalty', 'do_sample'):
                    if name in checkpoint_defaults:
                        defaults[name] = checkpoint_defaults[name]
                        origins[name] = 'Model default'
            model_config = Path(agent['model_path']) / 'config.json'
            model_type = json.loads(model_config.read_text()).get('model_type') if model_config.is_file() else None
            if model_type == 'qwen3' or 'enable_thinking' in selected:
                supported = supported | {'enable_thinking'}
                defaults['enable_thinking'] = selected.get('enable_thinking', True)
                origins['enable_thinking'] = 'Model configuration'
        elif selected['backend'] == 'api':
            # These are Chat Completions fields, not a claim about opaque server
            # internals. An endpoint rejection is surfaced without retry/fallback.
            supported |= {'temperature', 'top_p', 'seed'}
            defaults.update(temperature=0.7, top_p=0.9, seed=42)
            origins.update({name: 'Conversation default' for name in defaults if name not in origins})
            if 'reasoning_effort' in settings:
                defaults['reasoning_effort'] = settings['reasoning_effort']
                origins['reasoning_effort'] = 'Pipeline configuration'
                supported.add('reasoning_effort')
        validate_generation(defaults, supported)
        unsupported = {name: ('Thinking toggle requires the Qwen3 chat template.' if name == 'enable_thinking'
                              else 'This backend does not expose this control through its conversation interface.')
                       for name in GENERATION_FIELDS - supported}
        revision = selected.get('revision')
        if selected['backend'] == 'transformers' and revision is None:
            try:
                report = json.loads((Path(model_root) / 'download_report.json').read_text())
                revision = next((item.get('revision') for item in report['models']
                                 if item.get('repo_id') == selected['model_id']
                                 and item.get('status') == 'completed'
                                 and Path(item['local_dir']).resolve() == Path(agent['model_path'])), None)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        agents[agent['id']] = dict(agent=agent, generate=checked_generate, availability=availability,
                                  available=available, defaults=defaults, origins=origins, revision=revision,
                                  supported=sorted(supported), unsupported=unsupported)
        if source == config.get('conversation', {}).get('agent', 'local:qwen3-4b'):
            default = agent['id']
    if default is None:
        raise ValueError('Unknown conversational default agent: ' + config.get('conversation', {}).get('agent', 'local:qwen3-4b') + '; no fallback')
    return agents, default


def select_agent(memory, agents, identifier):
    """Persist an explicit selection without touching history or loading weights."""
    if not isinstance(identifier, str) or identifier not in agents:
        raise ValueError('Unknown agent configuration; select an agent from the list')
    if not agents[identifier].get('available', True):
        raise ValueError(agents[identifier]['availability'])
    memory.metadata['active_agent'] = deepcopy(agents[identifier]['agent'])
    memory.write_metadata()


def restore_agent(memory, agents, default):
    """Restore saved selection; older runs retain their recorded text provider."""
    saved = memory.metadata.get('active_agent')
    if saved:
        identifier = saved['id']
        if identifier not in agents or any(agents[identifier]['agent'].get(key) != value
                                          for key, value in saved.items() if key != 'label'):
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
    # Open unavailable runs so the GUI can explain the error and offer another agent.
    memory.metadata['active_agent'] = deepcopy(agents[identifier]['agent'])
    memory.write_metadata()



def generation_state(memory, agents):
    """Keep defaults and explicit overrides for each selected agent in this run."""
    identifier = memory.metadata['active_agent']['id']
    item = agents[identifier]
    profiles = memory.metadata.setdefault('generation_settings', {})
    if identifier not in profiles:
        profiles[identifier] = dict(defaults=deepcopy(item['defaults']), overrides={},
                                    origins=deepcopy(item['origins']))
    profile = profiles[identifier]
    effective = validate_generation(dict(profile['defaults'], **profile['overrides']), item['supported'])
    return dict(profile, values=effective, unsupported=item['unsupported'])


def update_generation(memory, agents, overrides):
    """Persist a full override map only after merged settings have been validated."""
    identifier = memory.metadata['active_agent']['id']
    item = agents[identifier]
    validate_generation(overrides, item['supported'])
    profile = generation_state(memory, agents)
    validate_generation(dict(profile['defaults'], **overrides), item['supported'])
    memory.metadata['generation_settings'][identifier]['overrides'] = deepcopy(overrides)
    memory.write_metadata()


def make_server(memory, generate, settings, port=8000, *, agents=None, root=None,
                configuration=None, default=None, lock=None, modelling=None):
    """Serialize selection and inference requests on the loopback interface."""
    root = Path(root or memory.directory.parent.parent).resolve()
    configuration = configuration if configuration is not None else memory.metadata['configuration']['pipeline']
    default = default or memory.metadata.get('active_agent', {}).get('id')
    if lock is None:
        lock = (memory.directory / 'conversation.md').open('rb')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        if agents:
            generation_state(memory, agents)
            memory.write_metadata()
    except Exception:
        lock.close()
        raise
    session_directories = {memory.directory}

    def runs():
        found = {}
        paths = set((root / 'runs').glob('*/metadata.yaml')) | {path / 'metadata.yaml' for path in session_directories}
        for path in paths:
            try:
                metadata = yaml.safe_load(path.read_text())
                if metadata.get('configuration', {}).get('kind') == 'conversation':
                    found[metadata['run_id']] = (path.parent, metadata)
            except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
                continue
        found[memory.metadata['run_id']] = (memory.directory, memory.metadata)
        return found

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
                        graph=graph_view(memory.directory),
                        modelling_error=memory.metadata.get('modelling_error'),
                        active_agent=memory.metadata.get('active_agent'),
                        generation=generation_state(memory, agents) if agents else None,
                        runs=[dict(run_id=key, started_at=meta['started_at'], status=meta['status'])
                              for key, (_, meta) in sorted(runs().items(), key=lambda pair: pair[1][1]['started_at'], reverse=True)],
                        agents=[dict(item['agent'], availability=item['availability'], available=item.get('available', True))
                                for item in (agents or {}).values()])

        def do_GET(self):
            if self.path == '/':
                self.reply(200, files('smpa_ros').joinpath('conversation.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path == '/api/sds':
                path = memory.directory / 'conversation-sds.md'
                self.reply(200, path.read_bytes() if path.exists() else b'# No SDS recorded yet\n', 'text/plain; charset=utf-8')
            elif self.path in ('/api/conversation', '/api/agents', '/api/graph'):
                self.reply(200, self.state())
            else:
                self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            nonlocal memory, lock
            if self.path not in ('/api/message', '/api/agent', '/api/settings', '/api/new', '/api/clear', '/api/resume'):
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
                if self.path in ('/api/new', '/api/clear', '/api/resume'):
                    if not agents:
                        raise ValueError('Conversation controls require configured agents')
                    candidate = None
                    candidate_lock = None
                    original = None
                    previous_metadata = deepcopy(memory.metadata)
                    try:
                        if self.path == '/api/resume':
                            entry = runs().get(data.get('run_id')) if isinstance(data.get('run_id'), str) else None
                            if entry is None:
                                raise ValueError('Unknown conversational run')
                            path, original = entry
                            if path == memory.directory:
                                self.reply(200, self.state())
                                return
                            if original['configuration']['pipeline'] != configuration:
                                raise ValueError('Resume requires the original pipeline configuration')
                            candidate_lock = (path / 'conversation.md').open('rb')
                            fcntl.flock(candidate_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            candidate = open_conversation(None, resume=path)
                            restore_agent(candidate, agents, default)
                        else:
                            candidate = open_conversation(root, configuration=configuration)
                            candidate_lock = (candidate.directory / 'conversation.md').open('rb')
                            fcntl.flock(candidate_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            if self.path == '/api/clear':
                                candidate.metadata['active_agent'] = deepcopy(memory.metadata['active_agent'])
                                candidate.metadata['generation_settings'] = deepcopy(memory.metadata.get('generation_settings', {}))
                            restore_agent(candidate, agents, default)
                        generation_state(candidate, agents)
                        candidate.write_metadata()
                        memory.finalize('completed')
                    except Exception:
                        memory.metadata = previous_metadata
                        try:
                            if candidate is not None:
                                if original is not None:
                                    candidate.metadata = original
                                    candidate.write_metadata()
                                else:
                                    candidate.finalize('failed')
                        finally:
                            if candidate_lock is not None:
                                candidate_lock.close()
                        raise
                    lock.close()
                    memory, lock = candidate, candidate_lock
                    session_directories.add(memory.directory)
                    self.server.memory, self.server.transcript_lock = memory, lock
                    self.reply(200, self.state())
                    return
                if self.path == '/api/agent' or 'agent_id' in data:
                    select_agent(memory, agents or {}, data.get('agent_id'))
                if self.path == '/api/settings':
                    if not agents:
                        raise ValueError('Generation settings require configured agents')
                    update_generation(memory, agents, data.get('overrides'))
                if self.path == '/api/message' and agents:
                    selected = agents[memory.metadata['active_agent']['id']]
                    if not selected.get('available', True):
                        raise ValueError(selected['availability'])
                    if 'overrides' in data:
                        update_generation(memory, agents, data['overrides'])
                    generation_state(memory, agents)
            except (ValueError, UnicodeError, OSError, KeyError, yaml.YAMLError) as error:
                self.reply(400, {'error': str(error)})
                return
            if self.path in ('/api/agent', '/api/settings'):
                if agents:
                    generation_state(memory, agents)
                    memory.write_metadata()
                self.reply(200, self.state())
                return
            try:
                if agents:
                    selected = agents[memory.metadata['active_agent']['id']]
                    profile = generation_state(memory, agents)
                    snapshot = dict(selected['agent'], model_revision=selected.get('revision'), generation_overrides=deepcopy(profile['overrides']),
                                    generation_defaults=deepcopy(profile['defaults']),
                                    generation_origins=deepcopy(profile['origins']))
                    exchange(memory, data['message'], selected['generate'], profile['values'], agent=snapshot, modelling=modelling)
                else:
                    exchange(memory, data['message'], generate, settings, modelling=modelling)
            except Exception:
                # Detailed backend errors live in the run record, not browser output.
                self.reply(500, dict(self.state(), error='Response failed. Your message is saved. Check the selected agent’s generation settings, credentials, local weights or service, and the run record. No fallback was used.'))
            else:
                self.reply(200, self.state())

        def log_message(self, format, *args):
            pass

    server = HTTPServer(('127.0.0.1', port), Handler)
    server.memory, server.transcript_lock = memory, lock
    original_close = server.server_close
    def close():
        server.transcript_lock.close()
        original_close()
    server.server_close = close
    return server


def main(args=None):
    """Load existing configuration, lock one run, and finalize it on shutdown."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.cwd() / 'config.yaml')
    parser.add_argument('--model-root', type=Path, help='Model storage root (default: models beside config)')
    parser.add_argument('--world-model-root', type=Path, default=Path.cwd() / 'memory')
    parser.add_argument('--resume', type=Path, help='Existing conversational run directory')
    parser.add_argument('--port', type=int, default=8000)
    options = parser.parse_args(args)
    options.model_root = options.model_root or options.config.resolve().parent / 'models'
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
    conversational = config.get('conversation', {})
    source = conversational.get('modelling_agent', 'local:qwen3-4b')
    selected_model = next((item for item in agents.values() if item['agent']['source'] == source), None)
    if selected_model is None:
        parser.error('Unknown conversational modelling agent: ' + source)
    ontology = conversational.get('ontology')
    if ontology:
        ontology = str((options.config.resolve().parent / ontology).resolve())
    model_settings = dict(selected_model['defaults'], max_new_tokens=2048)
    if 'do_sample' in selected_model['supported']:
        model_settings['do_sample'] = False
    modelling = dict(generate=selected_model['generate'], agent=selected_model['agent'],
                     settings=model_settings, ontology=ontology)
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
        server = make_server(memory, None, None, options.port, agents=agents, root=options.world_model_root,
                             configuration=config, default=default, lock=lock, modelling=modelling)
        print(f'Conversation: {memory.directory}\nOpen http://127.0.0.1:{server.server_port}', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        status = 'interrupted'
    except BaseException:
        status = 'failed'
        raise
    finally:
        if server is not None:
            memory = server.memory
        try:
            if memory is not None:
                memory.finalize(status)
        finally:
            if server is not None:
                server.server_close()
            if lock is not None:
                lock.close()
        signal.signal(signal.SIGTERM, previous)
        # The dedicated process exits here, releasing the lazy backend and model.


if __name__ == '__main__':
    main()
