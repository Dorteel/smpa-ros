"""Coordinate one Dialogue turn through SMPA's independent cognitive stages over ROS 2.

Concepts:
    A turn is what happens to one user message: Sense reads it, Model
    updates the beliefs, Plan forms the conversational response, Act
    delivers it. This order belongs to the Dialogue application. Each stage
    is a separate ROS 2 node that the coordinator calls on its own; the
    coordinator never reads, models, plans or acts itself, and it never
    edits beliefs.

    A stage adapter is the small, configurable boundary between the
    coordinator and one stage's ROS interface. ``PendingStage`` stands for a
    stage whose interface does not exist yet: it is never called and says
    so. ``RosStage`` calls a configured service or action: its request is
    filled from the turn's values by templates, and named outputs are read
    from its answer. Once Codex fixes the stage interfaces, connecting them
    is a matter of configuration (endpoint, type, request template, output
    fields), or of one adapter class when a stage needs more than that.

    Turn values are what a stage may be told: ``message``, ``turn_id``, the
    context identifiers ``context_mode``, ``run_id``, ``session_id`` and
    ``belief_context_id``, and the outputs of earlier stages:
    ``observation_id`` (Sense), ``model_invocation_id`` and
    ``belief_revision`` (Model), ``response`` (Plan) and ``delivery`` (Act).
    The turn record keeps them together, so the user message, the Sense
    observation, the Model invocation, the belief snapshot and the
    assistant response stay associated.

    The belief context says whose beliefs the Dialogue works on. In the
    shared context it is the robot's current run (the run reported by
    SMPA's ``/smpa/get_status`` service, or one chosen explicitly); in the
    independent context it is the Dialogue's own conversation session. Both
    are runs in SMPA's memory directory, read with the existing run and
    graph readers.

    The belief snapshot is read from the belief context's run only after
    Model has finished successfully; nothing is shown as a graph update
    before Model committed it. Its revision (a digest of the file) is
    compared with the one read when the turn started.

    Events (turn and stage started, completed, failed; belief snapshots)
    are published to every listener, which the web layer forwards over a
    WebSocket.
"""
import asyncio
import re
import string
import time
import uuid

from smpa_gui.ros_adapter.ros_clients import RosCallFailed

STAGES = ('sense', 'model', 'plan', 'act')
STAGE_TITLES = dict(sense='Sense', model='Model', plan='Plan', act='Act')
CONTEXT_VALUES = ('message', 'turn_id', 'context_mode', 'run_id', 'session_id', 'belief_context_id')
STAGE_OUTPUTS = dict(sense=('observation_id',), model=('model_invocation_id', 'belief_revision'),
                     plan=('response',), act=('delivery',))
REQUIRED_OUTPUTS = dict(plan=('response',))
CONTEXT_MODES = ('shared', 'independent')
TURNS_KEPT = 100
EVENTS_KEPT = 200
DEFAULT_TIMEOUT = 120.0
NAME = re.compile(r'^/[A-Za-z0-9_/~]*[A-Za-z0-9_]$')


class DialogueError(RuntimeError):
    """A Dialogue request cannot be served; ``status`` is the HTTP status to answer with."""

    def __init__(self, status, message):
        """Keep the status and the explanation."""
        super().__init__(message)
        self.status = status


class ConfigurationError(ValueError):
    """The ``dialogue`` section of the layout file is not valid; the message names the entry."""


def values_before(stage):
    """Return the turn values a stage's request may use: the context values and earlier stages' outputs."""
    earlier = STAGES[:STAGES.index(stage)]
    return set(CONTEXT_VALUES) | {name for previous in earlier for name in STAGE_OUTPUTS[previous]}


def template_names(template):
    """List the value names used by a request template: a string with {name} fields, or nested lists and mappings."""
    if isinstance(template, str):
        return {field for _, field, _, _ in string.Formatter().parse(template) if field}
    if isinstance(template, dict):
        return set().union(*(template_names(item) for item in template.values())) if template else set()
    if isinstance(template, list):
        return set().union(*(template_names(item) for item in template)) if template else set()
    return set()


def render(template, values):
    """Fill a request template from the turn values.

    A string that is exactly one field, such as "{message}", becomes the
    value itself, so numbers and lists keep their type. Any other string is
    formatted as text. Mappings and lists are filled element by element;
    numbers, booleans and None are taken literally.
    """
    if isinstance(template, str):
        single = re.fullmatch(r'\{(\w+)\}', template)
        if single:
            return values.get(single.group(1))
        return template.format_map({key: '' if value is None else value for key, value in values.items()})
    if isinstance(template, dict):
        return {key: render(item, values) for key, item in template.items()}
    if isinstance(template, list):
        return [render(item, values) for item in template]
    return template


def field(data, path):
    """Read a dotted field path such as "result.text" from JSON data; missing parts give None."""
    for part in path.split('.'):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


class PendingStage:
    """A stage whose ROS interface is not available yet; it is reported, never called."""

    def __init__(self, stage, reason=None):
        """Remember the stage and why it is pending."""
        self.stage = stage
        self.reason = reason or (f'The {STAGE_TITLES[stage]} stage interface is being defined by Codex; '
                                 'no ROS contract is configured for it yet.')

    def describe(self):
        """Describe the adapter for the API: it is pending and says why."""
        return dict(stage=self.stage, adapter='pending', configured=False, detail=self.reason)

    async def invoke(self, clients, values, on_event):
        """Refuse to run: a pending stage has no interface to call."""
        raise RosCallFailed('pending', self.reason)


class RosStage:
    """A stage reached through a configured ROS 2 service or action."""

    def __init__(self, stage, kind, endpoint, type_name, request, outputs, success=None, error=None,
                 timeout=DEFAULT_TIMEOUT):
        """Remember how to call the stage and how to read its answer.

        ``kind`` is "service" or "action"; ``endpoint`` the service or action
        name; ``type_name`` its installed interface type. ``request`` is the
        request or goal template. ``outputs`` maps turn value names to
        dotted fields of the response or result. ``success`` optionally
        names a boolean field that must be true, ``error`` a text field
        explaining a failure. ``timeout`` bounds the whole call.
        """
        self.stage, self.kind, self.endpoint, self.type_name = stage, kind, endpoint, type_name
        self.request, self.outputs, self.success, self.error, self.timeout = request, outputs, success, error, timeout

    def describe(self):
        """Describe the adapter for the API: endpoint, type and how values flow in and out."""
        return dict(stage=self.stage, adapter='ros', configured=True, kind=self.kind, endpoint=self.endpoint,
                    type=self.type_name, request=self.request, outputs=self.outputs, timeout_s=self.timeout,
                    detail=f'{self.kind} {self.endpoint} ({self.type_name})')

    async def invoke(self, clients, values, on_event):
        """Call the stage once and return its outputs, or raise ``RosCallFailed``.

        Fill the request template from the turn values and make the call.
        For an action, acceptance and feedback are passed to ``on_event``.
        A configured success field that is not true makes the stage
        ``failed`` with the configured error text. The configured outputs
        are then read from the answer.
        """
        request = render(self.request, values)
        if self.kind == 'service':
            answer = await clients.call_service(self.endpoint, self.type_name, request, timeout=self.timeout)
        else:
            outcome = await clients.run_action(self.endpoint, self.type_name, request, timeout=self.timeout,
                                               on_event=on_event)
            answer = outcome['result']
        if self.success and field(answer, self.success) is not True:
            reason = field(answer, self.error) if self.error else None
            raise RosCallFailed('failed', f'{STAGE_TITLES[self.stage]} reported a failure'
                                + (f': {reason}' if reason else ''), result=answer)
        return {name: field(answer, path) for name, path in self.outputs.items()}


def stage_adapter(stage, entry):
    """Build the adapter of one stage from its layout entry, checking every part.

    A missing entry or the word "pending" gives a ``PendingStage``. A
    mapping describes a ``RosStage``: ``kind`` (service or action),
    ``endpoint`` (an absolute ROS name), ``type`` (pkg/srv/Name or
    pkg/action/Name), ``request`` (a template using only values available
    before this stage), ``outputs`` (only this stage's output names), and
    optional ``success``, ``error`` and ``timeout_s``. Anything else is a
    ``ConfigurationError`` naming the problem.
    """
    if entry is None or entry == 'pending':
        return PendingStage(stage)
    if not isinstance(entry, dict):
        raise ConfigurationError(f'dialogue.stages.{stage} must be "pending" or a mapping')
    allowed = {'kind', 'endpoint', 'type', 'request', 'outputs', 'success', 'error', 'timeout_s'}
    unknown = set(entry) - allowed
    if unknown:
        raise ConfigurationError(f'dialogue.stages.{stage}: unknown entries {", ".join(sorted(unknown))}')
    kind = entry.get('kind')
    if kind not in ('service', 'action'):
        raise ConfigurationError(f'dialogue.stages.{stage}.kind must be "service" or "action"')
    endpoint, type_name = str(entry.get('endpoint') or ''), str(entry.get('type') or '')
    if not NAME.match(endpoint):
        raise ConfigurationError(f'dialogue.stages.{stage}.endpoint must be an absolute ROS name such as /dialogue/{stage}')
    middle = 'srv' if kind == 'service' else 'action'
    if not re.fullmatch(rf'\w+/{middle}/\w+', type_name):
        raise ConfigurationError(f'dialogue.stages.{stage}.type must look like package/{middle}/Name')
    request = entry.get('request') or {}
    if not isinstance(request, dict):
        raise ConfigurationError(f'dialogue.stages.{stage}.request must be a mapping of fields to templates')
    missing = template_names(request) - values_before(stage)
    if missing:
        raise ConfigurationError(f'dialogue.stages.{stage}.request uses values not available before {stage}: '
                                 f'{", ".join(sorted(missing))}')
    outputs = entry.get('outputs') or {}
    if not isinstance(outputs, dict) or set(outputs) - set(STAGE_OUTPUTS[stage]):
        raise ConfigurationError(f'dialogue.stages.{stage}.outputs may name only: {", ".join(STAGE_OUTPUTS[stage])}')
    for required in REQUIRED_OUTPUTS.get(stage, ()):
        if required not in outputs:
            raise ConfigurationError(f'dialogue.stages.{stage}.outputs must say where to find {required}')
    timeout = float(entry.get('timeout_s', DEFAULT_TIMEOUT))
    if not 0.1 <= timeout <= 3600:
        raise ConfigurationError(f'dialogue.stages.{stage}.timeout_s must be between 0.1 and 3600')
    return RosStage(stage, kind, endpoint, type_name, request, {key: str(value) for key, value in outputs.items()},
                    success=entry.get('success'), error=entry.get('error'), timeout=timeout)


def configuration(section):
    """Turn the layout file's ``dialogue`` section into the context mode and the four stage adapters.

    Every stage defaults to pending and the context to independent, so a
    layout without a ``dialogue`` section describes today's state exactly.
    """
    section = section or {}
    if not isinstance(section, dict) or set(section) - {'context', 'stages'}:
        raise ConfigurationError('dialogue accepts only: context, stages')
    mode = section.get('context', 'independent')
    if mode not in CONTEXT_MODES:
        raise ConfigurationError('dialogue.context must be "shared" or "independent"')
    stages = section.get('stages') or {}
    if not isinstance(stages, dict) or set(stages) - set(STAGES):
        raise ConfigurationError(f'dialogue.stages accepts only: {", ".join(STAGES)}')
    return dict(context=mode, adapters={stage: stage_adapter(stage, stages.get(stage)) for stage in STAGES})


class DialogueCoordinator:
    """Run Dialogue turns stage by stage over ROS 2 and publish what happens."""

    def __init__(self, clients, adapters, context_mode, read_belief, robot_run, conversation_session):
        """Prepare a coordinator with nothing running.

        ``clients`` makes the ROS calls (``RosClients``); ``adapters`` maps
        each stage to its adapter. ``read_belief(run_id)`` reads the belief
        snapshot of a run with the existing graph reader. ``robot_run()`` is
        a coroutine returning the robot's current run id or None (from
        SMPA's run-control service). ``conversation_session()`` returns the
        Dialogue's current conversation session id or None. The context
        starts in the configured mode with nothing chosen explicitly.
        """
        self.clients = clients
        self.adapters = adapters
        self.read_belief = read_belief
        self.robot_run = robot_run
        self.conversation_session = conversation_session
        self.context = dict(mode=context_mode, run_id=None, session_id=None)
        self.turns = {}
        self.order = []
        self.requests = {}
        self.active = None
        self.task = None
        self.listeners = set()
        self.history = []

    # ----- description -------------------------------------------------------------------------

    def stages(self):
        """Describe the four stages' adapters in order."""
        return [self.adapters[stage].describe() for stage in STAGES]

    def ros_path_ready(self):
        """Tell whether every stage is connected to a ROS interface (none is pending)."""
        return all(not isinstance(self.adapters[stage], PendingStage) for stage in STAGES)

    def status(self):
        """Describe the Dialogue backend: which path answers messages, the stages, the context and the turns.

        While any stage is pending, messages keep going through SMPA's
        conversation server (``path`` is "conversation_server"); once all four
        stages are configured, the ROS path can run turns.
        """
        pending = [stage for stage in STAGES if isinstance(self.adapters[stage], PendingStage)]
        return dict(path='ros' if not pending else 'conversation_server', ros_path_ready=not pending,
                    pending_stages=pending, stages=self.stages(), context=dict(self.context),
                    active_turn=self.active, turns=[self.summary(self.turns[turn_id]) for turn_id in self.order[-20:]])

    def summary(self, turn):
        """Return a copy of a turn record that callers may keep."""
        return {key: (dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value)
                for key, value in turn.items()}

    def get(self, turn_id):
        """Return one turn record, or raise a 404 ``DialogueError``."""
        if turn_id not in self.turns:
            raise DialogueError(404, 'Unknown turn')
        return self.summary(self.turns[turn_id])

    # ----- context ------------------------------------------------------------------------------

    def choose_context(self, mode, run_id=None, session_id=None):
        """Choose the shared or independent belief context, optionally naming the run and the session.

        In the shared context ``run_id`` names the robot's run to share; left
        out, the run reported by SMPA's run control is used at each turn. In
        the independent context the Dialogue's own session (``session_id``,
        or the current conversation session) is the belief context. A turn
        that is running keeps the context it started with.
        """
        if mode not in CONTEXT_MODES:
            raise DialogueError(422, 'The context must be "shared" or "independent"')
        self.context = dict(mode=mode, run_id=run_id or None, session_id=session_id or None)
        return dict(self.context)

    async def resolve_context(self):
        """Work out the identifiers a turn carries, from the chosen context.

        The Dialogue's session is the explicitly chosen one or the current
        conversation session. Independent: the belief context is that
        session (it is also the run). Shared: the belief context is the
        chosen robot run or, failing that, the run SMPA's run control
        reports. Without a run to use, the turn cannot start (409).
        """
        session = self.context['session_id'] or self.conversation_session()
        if self.context['mode'] == 'independent':
            if not session:
                raise DialogueError(409, 'The independent context needs a Dialogue session: start or open a conversation')
            return dict(context_mode='independent', run_id=session, session_id=session, belief_context_id=session)
        run = self.context['run_id'] or await self.robot_run()
        if not run:
            raise DialogueError(409, 'The shared context needs the robot\'s run: none is running according to '
                                     'SMPA\'s run control, and none was chosen')
        return dict(context_mode='shared', run_id=run, session_id=session, belief_context_id=run)

    # ----- events -------------------------------------------------------------------------------

    def listen(self):
        """Register a listener and return its queue, pre-filled with recent events so it can catch up."""
        queue = asyncio.Queue(maxsize=EVENTS_KEPT * 2)
        for event in self.history[-20:]:
            queue.put_nowait(event)
        self.listeners.add(queue)
        return queue

    def unlisten(self, queue):
        """Remove a listener; its queue receives nothing more."""
        self.listeners.discard(queue)

    def publish(self, event, **fields):
        """Send one event to every listener and keep it in the recent history.

        A listener that does not keep up loses its oldest events rather than
        slowing the turn down.
        """
        message = dict(event=event, at=time.time(), **fields)
        self.history = (self.history + [message])[-EVENTS_KEPT:]
        for queue in list(self.listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    # ----- turns --------------------------------------------------------------------------------

    def new_turn(self, message, values, start_stage='sense', retry_of=None):
        """Create and remember a turn record with every stage waiting."""
        turn_id = uuid.uuid4().hex[:16]
        values = dict(values, message=message, turn_id=turn_id)
        turn = dict(turn_id=turn_id, message=message, state='running', created_at=time.time(), finished_at=None,
                    retry_of=retry_of, failed_stage=None, error=None, values=values, belief_before=None, belief=None,
                    stages={stage: dict(state='skipped' if STAGES.index(stage) < STAGES.index(start_stage) else 'waiting',
                                        started_at=None, finished_at=None, outputs={}, error=None)
                            for stage in STAGES})
        self.turns[turn_id] = turn
        self.order.append(turn_id)
        while len(self.order) > TURNS_KEPT:
            self.turns.pop(self.order.pop(0), None)
        return turn

    def check_free(self, request_id):
        """Refuse a new turn while one runs, and recognise a repeated request id; return an existing record or None."""
        if request_id in self.requests and self.requests[request_id] in self.turns:
            return dict(self.get(self.requests[request_id]), duplicate=True)
        if self.active is not None:
            raise DialogueError(409, 'A Dialogue turn is still running; wait for it to finish')
        return None

    async def submit(self, message, request_id):
        """Start a turn for a user message over ROS 2 and return its record at once.

        The turn runs in the background; its progress is published as
        events and kept in the record. It is refused (409) while any stage
        is pending, because the ROS path is not complete; messages then go
        through SMPA's conversation server instead. Only one turn runs at a
        time, and a repeated request id returns the first record.
        """
        existing = self.check_free(request_id)
        if existing:
            return existing
        if not self.ros_path_ready():
            pending = ', '.join(stage for stage in STAGES if isinstance(self.adapters[stage], PendingStage))
            raise DialogueError(409, f'The ROS path is not available yet; pending stage interfaces: {pending}. '
                                     'Messages go through SMPA\'s conversation server.')
        if not message.strip():
            raise DialogueError(422, 'The message is empty')
        values = await self.resolve_context()
        turn = self.new_turn(message, values)
        self.requests[request_id] = turn['turn_id']
        self.start(turn, 'sense')
        return self.summary(turn)

    async def retry(self, turn_id, request_id):
        """Run a failed turn again from the stage that failed, reusing what the earlier stages produced.

        Stages that succeeded are not called again, so a retry neither
        repeats the Sense observation nor applies the Model update twice.
        The new turn keeps the original context identifiers and refers to
        the failed one with ``retry_of``.
        """
        existing = self.check_free(request_id)
        if existing:
            return existing
        failed = self.turns.get(turn_id)
        if failed is None:
            raise DialogueError(404, 'Unknown turn')
        if failed['state'] != 'failed':
            raise DialogueError(409, 'Only a failed turn can be retried')
        values = {key: value for key, value in failed['values'].items() if key not in ('message', 'turn_id')}
        turn = self.new_turn(failed['message'], values, start_stage=failed['failed_stage'], retry_of=turn_id)
        for stage in STAGES[:STAGES.index(failed['failed_stage'])]:
            turn['stages'][stage]['outputs'] = dict(failed['stages'][stage]['outputs'])
        turn['belief'] = failed['belief']
        self.requests[request_id] = turn['turn_id']
        self.start(turn, failed['failed_stage'])
        return self.summary(turn)

    def start(self, turn, first):
        """Mark the turn active and run it as a background task of the event loop."""
        self.active = turn['turn_id']
        self.task = asyncio.get_running_loop().create_task(self.execute(turn, first))

    async def execute(self, turn, first):
        """Run the stages from ``first`` to Act, one after the other, and record and publish each step.

        Before Model runs, the belief snapshot of the belief context is read
        so a change can be recognised. Each stage is called through its
        adapter with the turn values; its outputs become turn values for the
        later stages. After Model succeeds the belief snapshot is read again
        and published. The first failing stage ends the turn as failed with
        its reason; the later stages are not called. Whatever happens, the
        coordinator is free for the next turn afterwards.
        """
        values = turn['values']
        self.publish('turn_started', turn_id=turn['turn_id'], message=turn['message'], retry_of=turn['retry_of'],
                     context={key: values.get(key) for key in CONTEXT_VALUES[2:]})
        try:
            for stage in STAGES[STAGES.index(first):]:
                if stage == 'model':
                    turn['belief_before'] = self.snapshot(values['belief_context_id'])
                record = turn['stages'][stage]
                record.update(state='running', started_at=time.time())
                self.publish('stage_started', turn_id=turn['turn_id'], stage=stage)

                def forward(kind, details, stage=stage):
                    """Publish an action's acceptance or feedback as a stage event."""
                    self.publish(f'stage_{kind}', turn_id=turn['turn_id'], stage=stage, **details)
                try:
                    outputs = await self.adapters[stage].invoke(self.clients, values, forward)
                    missing = [name for name in REQUIRED_OUTPUTS.get(stage, ()) if outputs.get(name) in (None, '')]
                    if missing:
                        raise RosCallFailed('failed', f'{STAGE_TITLES[stage]} returned no {", ".join(missing)}')
                except RosCallFailed as error:
                    record.update(state=error.state, finished_at=time.time(), error=str(error))
                    turn.update(state='failed', failed_stage=stage, error=str(error), finished_at=time.time())
                    self.publish('stage_failed', turn_id=turn['turn_id'], stage=stage, state=error.state, error=str(error))
                    self.publish('turn_failed', turn_id=turn['turn_id'], stage=stage, error=str(error))
                    return
                record.update(state='succeeded', finished_at=time.time(), outputs=outputs)
                values.update({name: value for name, value in outputs.items() if value is not None})
                self.publish('stage_completed', turn_id=turn['turn_id'], stage=stage, outputs=outputs)
                if stage == 'model':
                    after = self.snapshot(values['belief_context_id'])
                    before = turn['belief_before'] or {}
                    turn['belief'] = dict(after, changed=after.get('revision') != before.get('revision'),
                                          model_invocation_id=values.get('model_invocation_id'))
                    self.publish('belief_snapshot', turn_id=turn['turn_id'], **turn['belief'])
            turn.update(state='completed', finished_at=time.time())
            self.publish('turn_completed', turn_id=turn['turn_id'], response=values.get('response'),
                         observation_id=values.get('observation_id'),
                         model_invocation_id=values.get('model_invocation_id'),
                         belief_revision=(turn['belief'] or {}).get('revision'))
        except Exception as error:  # noqa: BLE001 - an unexpected failure must still free the coordinator
            turn.update(state='failed', error=f'Unexpected error: {error}', finished_at=time.time())
            self.publish('turn_failed', turn_id=turn['turn_id'], error=turn['error'])
        finally:
            self.active = None

    def snapshot(self, belief_context_id):
        """Read the belief snapshot of a belief context: its run, file, state, revision and triple count."""
        try:
            reading = self.read_belief(belief_context_id)
        except Exception as error:  # noqa: BLE001 - a missing run is a state of the snapshot
            return dict(belief_context_id=belief_context_id, state='unavailable', detail=str(error), revision=None)
        return dict(belief_context_id=belief_context_id, state=reading.get('state'), path=reading.get('path'),
                    revision=reading.get('revision'), triples=reading.get('triples'))

    async def shutdown(self):
        """Stop a running turn when the GUI stops; its record says it was interrupted."""
        if self.task is not None and not self.task.done():
            turn = self.turns.get(self.active) if self.active else None
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - ending anyway
                pass
            if turn is not None and turn['state'] == 'running':
                turn.update(state='interrupted', finished_at=time.time())
        self.active = None
