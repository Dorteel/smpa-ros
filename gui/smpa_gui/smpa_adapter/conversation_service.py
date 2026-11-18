"""Run SMPA's own conversation server on demand and relay the GUI's conversation requests to it.

Concepts:
    The conversation itself (inference, transcript, run lifecycle, belief
    modelling) belongs to SMPA's ``smpa_ros.conversation_web``. The GUI does
    not import it and implements no inference. This module is an adapter:

    - In managed mode it starts that server as a child process with SMPA's
      own interpreter, configuration, model root and memory root from the
      layout, on a loopback port, the first time a conversation is started or
      reopened, and stops it (SIGINT, so SMPA finalizes its run) when the GUI
      stops. Its output is kept as a short log for display.
    - In external mode (``conversation_url``) it talks to a server someone
      else started and never starts or stops it.

    SMPA's server handles one request at a time and serves one run per
    process. Starting the managed server creates a new run unless it is
    started with ``--resume``; on a running server, ``/api/new`` and
    ``/api/resume`` switch runs. A model is selected by posting SMPA's agent
    id for the chosen source (``local:<name>`` or ``api:<provider>``).

    Only one operation runs at a time. While a reply is being generated the
    server cannot answer, so the status reported to the GUI comes from the
    last known state plus the pending operation. Repeating a request id
    returns the first outcome instead of repeating the operation.
"""
from collections import OrderedDict, deque
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from smpa_gui.smpa_adapter import conversation_transcript, smpa_layout, smpa_models

START_TIMEOUT = 180.0
MESSAGE_TIMEOUT = 900.0


class ConversationError(Exception):
    """A conversation request could not be carried out; ``status`` is the HTTP status to report."""

    def __init__(self, status, message, state=None):
        super().__init__(message)
        self.status = status
        self.state = state


def slim_state(state):
    """Keep the parts of SMPA's conversation state the GUI shows, in the transcript reader's message shape."""
    messages = []
    for index, message in enumerate(state.get('messages') or [], start=1):
        messages.append(dict(index=index, role=message.get('role'), content=message.get('content', ''),
                             thinking=message.get('thinking'),
                             agent=conversation_transcript.agent_summary(message.get('agent'))))
    agents = [dict(source=agent.get('source'), id=agent.get('id'), label=agent.get('label'),
                   available=agent.get('available', True), availability=agent.get('availability'))
              for agent in state.get('agents') or []]
    active = state.get('active_agent') if isinstance(state.get('active_agent'), dict) else None
    return dict(run_id=state.get('run_id'), messages=messages, agents=agents,
                active_agent=dict(id=active.get('id'), source=active.get('source'), label=active.get('label'))
                if active else None, modelling_error=state.get('modelling_error'))


class ConversationService:
    """Own at most one SMPA conversation server and relay requests to it, one at a time."""

    def __init__(self, layout, environment=None):
        """Prepare, without starting anything, from the GUI layout (see ``smpa_layout``)."""
        self.layout = layout
        self.environment = dict(os.environ if environment is None else environment)
        self.external = layout.get('conversation_url')
        self.url = (self.external or f'http://127.0.0.1:{layout["conversation_port"]}').rstrip('/')
        self.process = None
        self.log = deque(maxlen=300)
        self.error = None
        self.exit_code = None
        self.phase = 'stopped'
        self.operation = None
        self.snapshot = None
        self.busy = threading.Lock()
        self.requests = OrderedDict()
        self.requests_lock = threading.Lock()

    # ----- talking to SMPA's server -------------------------------------------------------------------

    def http(self, method, path, body=None, timeout=10.0):
        """Send one request to the conversation server and return ``(status, JSON body)``.

        No Origin header is sent: the request comes from this program, not
        from a browser page. Connection failures raise ``ConversationError``
        with status 503.
        """
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b'{}')
            except ValueError:
                return error.code, {'error': f'HTTP {error.code} from the conversation server'}
        except (urllib.error.URLError, OSError, ValueError) as error:
            reason = getattr(error, 'reason', error)
            raise ConversationError(503, f'The SMPA conversation server at {self.url} is not reachable: {reason}') from error

    def call(self, method, path, body=None, timeout=10.0):
        """Send a request, remember the returned state, and turn SMPA's error replies into ``ConversationError``."""
        status, payload = self.http(method, path, body, timeout)
        if isinstance(payload, dict) and 'messages' in payload:
            self.snapshot = slim_state(payload)
        if status >= 400:
            raise ConversationError(502 if status >= 500 else 409, payload.get('error') or f'SMPA answered {status}',
                                    self.snapshot if 'messages' in payload else None)
        return self.snapshot

    # ----- managed process ----------------------------------------------------------------------------

    def running(self):
        """Tell whether the managed server process is alive (always False in external mode)."""
        return self.process is not None and self.process.poll() is None

    def prerequisites(self):
        """List what is missing to start SMPA's conversation server from the layout; empty when ready."""
        missing = []
        if self.layout['smpa_root'] is None:
            missing.append('an SMPA checkout (set smpa_root, for example ros2 launch smpa_gui gui.launch.py '
                           'smpa_root:=$HOME/ros2_ws/src/smpa-ros)')
            return missing
        if smpa_layout.checkout_layout(self.layout['smpa_root']) is None:
            missing.append(f'SMPA checkout with smpa_ros/smpa_ros/conversation_web.py at {self.layout["smpa_root"]} (set smpa_root)')
        if not Path(self.layout['python']).is_file():
            missing.append(f"SMPA's Python interpreter at {self.layout['python']} (set python)")
        if not Path(self.layout['config']).is_file():
            missing.append(f"SMPA's configuration at {self.layout['config']} (set config)")
            return missing
        if self.needs_installed_smpa_llm() and smpa_models.package_share('smpa_llm') is None:
            missing.append('the ROS package smpa_llm in the sourced workspace (SMPA resolves its inference defaults and '
                           'model registry through it): build SMPA with its install.sh and source '
                           '~/ros2_ws/install/setup.bash before starting the GUI')
        return missing

    def needs_installed_smpa_llm(self):
        """Tell whether SMPA will look up the installed smpa_llm package for this configuration.

        That is the case when the configuration selects
        ``package://smpa_llm/config.yaml`` or has no model registry beside it.
        """
        import yaml
        config = Path(self.layout['config'])
        try:
            content = yaml.safe_load(config.read_text(encoding='utf-8')) or {}
        except (OSError, yaml.YAMLError):
            return False
        selects = isinstance(content, dict) and content.get('inference_config') == 'package://smpa_llm/config.yaml'
        return selects or not (config.resolve().parent / 'models' / 'model_library.yaml').is_file()

    def port_in_use(self):
        """Tell whether something already listens on the managed server's port."""
        with socket.socket() as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(('127.0.0.1', int(self.layout['conversation_port']))) == 0

    def read_output(self, process):
        """Copy the child's output into the log until it closes."""
        for line in iter(process.stdout.readline, ''):
            self.log.append(line.rstrip('\n'))
        process.stdout.close()

    def launch(self, resume=None):
        """Start SMPA's conversation server and wait until it answers; raise with its output if it does not.

        Without ``resume`` SMPA creates a new conversation run; with a run
        directory it reopens that run.
        """
        missing = self.prerequisites()
        if missing:
            raise ConversationError(409, 'Cannot start the SMPA conversation server. Missing: ' + '; '.join(missing))
        if self.port_in_use():
            raise ConversationError(409, f'Port {self.layout["conversation_port"]} is already in use. Another conversation '
                                    'server may be running; set conversation_url to use it, or choose another conversation_port.')
        command = [str(self.layout['python']), '-m', 'smpa_ros.conversation_web', '--config', str(self.layout['config']),
                   '--world-model-root', str(self.layout['memory_root']), '--model-root', str(self.layout['model_root']),
                   '--port', str(self.layout['conversation_port'])]
        if resume:
            command += ['--resume', str(resume)]
        environment = dict(self.environment)
        # SMPA's modules come from its checkout (packages layout: smpa_ros and smpa_llm folders), ahead of
        # any installed copy; ROS packages such as smpa_llm's share folder come from the sourced workspace.
        sources = [str(path) for path in smpa_layout.python_path(self.layout['smpa_root'])]
        environment['PYTHONPATH'] = os.pathsep.join(filter(None, sources + [environment.get('PYTHONPATH')]))
        environment['PYTHONUNBUFFERED'] = '1'
        self.log.append('$ ' + ' '.join(command))
        self.phase, self.error, self.exit_code, self.snapshot = 'starting', None, None, None
        self.process = subprocess.Popen(command, cwd=str(self.layout['smpa_root']), env=environment, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        threading.Thread(target=self.read_output, args=(self.process,), daemon=True).start()
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                time.sleep(0.2)
                self.exit_code = self.process.returncode
                tail = [line for line in list(self.log)[-8:] if line.strip()]
                self.phase, self.error = 'failed', (f'The SMPA conversation server exited with code {self.exit_code}. '
                                                    + (tail[-1] if tail else ''))
                raise ConversationError(502, self.error)
            try:
                self.call('GET', '/api/conversation', timeout=2.0)
                self.phase = 'ready'
                return
            except ConversationError:
                time.sleep(0.3)
        self.stop_process()
        self.phase, self.error = 'failed', f'The SMPA conversation server did not answer within {START_TIMEOUT:.0f} s.'
        raise ConversationError(504, self.error)

    def stop_process(self):
        """Stop the managed server: SIGINT first so SMPA finalizes its run, then SIGTERM, then kill."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        for sending, wait in ((signal.SIGINT, 15), (signal.SIGTERM, 5)):
            process.send_signal(sending)
            try:
                process.wait(wait)
                return
            except subprocess.TimeoutExpired:
                continue
        process.kill()
        process.wait(5)

    # ----- operations ---------------------------------------------------------------------------------

    def exclusive(self, kind, work, **details):
        """Run one operation at a time; refuse with 409 while another one is in progress."""
        if not self.busy.acquire(blocking=False):
            current = self.operation or {}
            raise ConversationError(409, f'The conversation is busy ({current.get("kind", "working")}); try again when it finishes.')
        self.operation = dict(kind=kind, started_at=time.time(), **details)
        try:
            return work()
        finally:
            self.operation = None
            self.busy.release()

    def once(self, request_id, work):
        """Run ``work`` once per request id; a repeated id waits for and returns the first outcome."""
        if not request_id:
            return work()
        with self.requests_lock:
            entry = self.requests.get(request_id)
            owner = entry is None
            if owner:
                entry = self.requests[request_id] = dict(done=threading.Event(), result=None, error=None)
                while len(self.requests) > 64:
                    self.requests.popitem(last=False)
        if owner:
            try:
                entry['result'] = work()
            except ConversationError as error:
                entry['error'] = error
            finally:
                entry['done'].set()
        else:
            entry['done'].wait(MESSAGE_TIMEOUT)
        if entry['error'] is not None:
            raise entry['error']
        return entry['result']

    def ensure_server(self, resume=None):
        """Make sure a server is answering; start the managed one if needed. Return True if it was just started."""
        if self.external:
            self.call('GET', '/api/conversation', timeout=5.0)
            self.phase = 'ready'
            return False
        if self.running():
            return False
        self.launch(resume=resume)
        return True

    def select(self, source):
        """Make SMPA's agent for a model source (``local:…`` or ``api:…``) the active one."""
        if not source:
            return self.snapshot
        agent = next((item for item in (self.snapshot or {}).get('agents', []) if item['source'] == source), None)
        if agent is None:
            raise ConversationError(404, f'SMPA offers no conversation agent for {source}')
        if (self.snapshot.get('active_agent') or {}).get('id') == agent['id']:
            return self.snapshot
        return self.call('POST', '/api/agent', {'agent_id': agent['id']})

    def start(self, source=None, request_id=None):
        """Start a new conversation run, then select the model. Starting the server itself creates the run."""
        def work():
            if not self.ensure_server():
                self.call('POST', '/api/new', {})
            return self.select(source)
        return self.once(request_id, lambda: self.exclusive('start', work))

    def resume(self, run_id, directory, source=None, request_id=None):
        """Reopen an existing conversation run so it can be continued, optionally switching the model."""
        def work():
            if not self.ensure_server(resume=directory):
                self.call('POST', '/api/resume', {'run_id': run_id})
            return self.select(source)
        return self.once(request_id, lambda: self.exclusive('resume', work, run_id=run_id))

    def change_model(self, source):
        """Switch the active conversation's model; SMPA records the change in the run."""
        def work():
            self.ensure_server_running()
            return self.select(source)
        return self.exclusive('model', work)

    def ensure_server_running(self):
        """Require a server that is already serving a conversation (without starting one)."""
        if self.external:
            if self.snapshot is None:
                self.call('GET', '/api/conversation', timeout=5.0)
            return
        if not self.running():
            raise ConversationError(409, 'No conversation is active. Start or reopen one first.')

    def send(self, text, request_id=None):
        """Send one user message to the active conversation and return the state with SMPA's reply.

        SMPA saves the user's message before generating; if generation fails it
        answers with an error and the message stays saved. That error is
        passed on with the state, so the GUI can show both.
        """
        def work():
            self.ensure_server_running()
            return self.call('POST', '/api/message', {'message': text}, timeout=MESSAGE_TIMEOUT)
        return self.once(request_id, lambda: self.exclusive('message', work, text=text))

    def refresh(self):
        """Fetch the current state when nothing else is running; otherwise keep the last known one."""
        if self.busy.locked() or (not self.external and not self.running()):
            return self.snapshot
        if not self.busy.acquire(blocking=False):
            return self.snapshot
        try:
            self.call('GET', '/api/conversation', timeout=3.0)
            if self.external:
                self.phase, self.error = 'ready', None
        except ConversationError as error:
            if self.external:
                self.phase, self.error = 'unreachable', str(error)
        finally:
            self.busy.release()
        return self.snapshot

    def stop(self):
        """Stop the managed server (finalizing its run); in external mode only forget the state."""
        def work():
            if not self.external:
                self.stop_process()
            self.phase, self.snapshot = 'stopped', None
            return None
        return self.exclusive('stop', work)

    def shutdown(self):
        """Stop the managed server when the GUI exits, waiting for a running operation only briefly."""
        if not self.external:
            self.stop_process()

    def status(self, refresh=True):
        """Describe the server and the active conversation for the GUI, without blocking on SMPA."""
        if refresh:
            self.refresh()
        if not self.external:
            if self.process is not None and self.process.poll() is not None and self.phase in ('ready', 'starting'):
                self.exit_code = self.process.returncode
                tail = [line for line in list(self.log)[-8:] if line.strip()]
                self.phase, self.error = 'failed', f'The SMPA conversation server stopped (exit code {self.exit_code}). ' + (
                    tail[-1] if tail else '')
        phase = 'busy' if self.operation else self.phase
        operation = dict(self.operation, elapsed_s=round(time.time() - self.operation['started_at'], 1)) if self.operation else None
        snapshot = self.snapshot or {}
        return dict(mode='external' if self.external else 'managed', url=self.url, phase=phase, operation=operation,
                    pid=self.process.pid if self.running() else None, error=self.error, exit_code=self.exit_code,
                    run_id=snapshot.get('run_id') if phase not in ('stopped', 'failed') else None,
                    active_agent=snapshot.get('active_agent'), agents=snapshot.get('agents', []),
                    modelling_error=snapshot.get('modelling_error'),
                    prerequisites_missing=[] if self.external else self.prerequisites(), log=list(self.log)[-60:])
