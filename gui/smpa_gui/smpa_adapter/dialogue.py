"""Coordinate one cognitive step of the robot's run through SMPA's four stage actions, for the Dialogue.

Concepts:
    The Dialogue has two parts. Its conversation goes through SMPA's own
    conversation server (``conversation_service``), which saves the
    transcript, replies, and has SMPA's Model stage update the conversation's
    beliefs. Its cognitive step is this module: Sense → Model → Plan → Act
    on a running robot run, called through SMPA's stage actions
    ``/smpa/sense``, ``/smpa/model``, ``/smpa/plan`` and ``/smpa/act``
    (``smpa_ros_interfaces/action/Sense``, ``Model``, ``Plan``, ``Act``).
    The coordinator only calls them in this order and passes each result
    on; it never senses, models, plans, acts or edits beliefs itself.

    A turn is one such step. Its values are what a stage may be told:
    ``run_id`` (the robot's run), ``context_mode``, ``session_id`` (the
    Dialogue's conversation), ``belief_context_id``, ``turn_id``,
    ``previous_snapshot`` (the latest world-model snapshot of the run, as
    SMPA's experiment passes it), an optional user ``message``, and the
    outputs of earlier stages: Sense ``observation`` and ``sense_record``;
    Model ``world_model``, ``model_record`` and ``snapshot_path``; Plan
    ``action_json`` and ``plan_record``; Act ``act_success`` and
    ``act_record``. The record paths are SMPA's own persisted records, so a
    turn record associates the message, the observation, the Model
    invocation, the world-model snapshot, the plan and the execution.

    A stage adapter is the configurable boundary to one stage. The default
    adapters use SMPA's merged contracts (``SMPA_STAGES``); the layout file's
    ``dialogue.stages`` section may rename an endpoint, change a request
    template or mark a stage ``pending``. ``RosStage`` fills its request
    from turn values by templates and reads named outputs from the result.

    Belief contexts: the stage actions work on a robot run that SMPA's
    experiment is running (it creates cycles and holds the inference
    freeze). In the shared context that run is the robot's current run
    (``/smpa/get_status``) or one chosen explicitly, and steps can run. In
    the independent context the Dialogue's conversation keeps its own
    beliefs through the conversation server; no robot run is involved, so
    a cognitive step is refused with an explanation.

    Acting can move the robot. Unless the layout turns it off, a turn stops
    before Act with the plan's decision and waits for a local user to
    approve or decline it (``decide``); declining ends the turn without
    calling Act.

    Snapshots are read only after Model succeeded: the world-model snapshot
    Model reports and the run's belief graph (with the graph reader of the
    graph views). Nothing is shown as a model update before Model committed it.

    Events (turn and stage started, accepted, feedback, completed, failed;
    act confirmation required; snapshots) are published to listeners,
    which the web layer forwards over a WebSocket.
"""
import asyncio
import json
import re
import string
import time
import uuid

from smpa_gui.ros_adapter.ros_clients import RosCallFailed

STAGES = ('sense', 'model', 'plan', 'act')
STAGE_TITLES = dict(sense='Sense', model='Model', plan='Plan', act='Act')
CONTEXT_VALUES = ('message', 'turn_id', 'context_mode', 'run_id', 'session_id', 'belief_context_id', 'previous_snapshot')
STAGE_OUTPUTS = dict(sense=('observation', 'sense_record'), model=('world_model', 'model_record', 'snapshot_path'),
                     plan=('action_json', 'plan_record'), act=('act_success', 'act_record'))
REQUIRED_OUTPUTS = dict(sense=('sense_record',), model=('model_record',), plan=('action_json', 'plan_record'))
CONTEXT_MODES = ('shared', 'independent')
TURNS_KEPT = 100
EVENTS_KEPT = 200
DEFAULT_TIMEOUT = 600.0
NAME = re.compile(r'^/[A-Za-z0-9_/~]*[A-Za-z0-9_]$')

# SMPA's merged stage contracts (smpa_ros_interfaces/action/{Sense,Model,Plan,Act}.action, served by
# smpa_ros/{sense,model,plan,act}_node.py). The request fields and their order follow SMPA's experiment
# (smpa_ros/smpa.py run_actions): an empty capability selects each node's default.
SMPA_STAGES = {
    'sense': dict(kind='action', endpoint='/smpa/sense', type='smpa_ros_interfaces/action/Sense',
                  request={'run_id': '{run_id}', 'capability': '', 'prompt_name': ''},
                  outputs={'observation': 'observation', 'sense_record': 'record_path'}),
    'model': dict(kind='action', endpoint='/smpa/model', type='smpa_ros_interfaces/action/Model',
                  request={'run_id': '{run_id}', 'capability': '', 'prompt_name': '', 'observation': '{observation}',
                           'source_record': '{sense_record}', 'previous_snapshot': '{previous_snapshot}',
                           'messages_json': ''},
                  outputs={'world_model': 'world_model', 'model_record': 'record_path', 'snapshot_path': 'snapshot_path'}),
    'plan': dict(kind='action', endpoint='/smpa/plan', type='smpa_ros_interfaces/action/Plan',
                 request={'run_id': '{run_id}', 'capability': '', 'prompt_name': '', 'world_model': '{world_model}',
                          'source_record': '{model_record}'},
                 outputs={'action_json': 'action_json', 'plan_record': 'record_path'}),
    'act': dict(kind='action', endpoint='/smpa/act', type='smpa_ros_interfaces/action/Act',
                request={'run_id': '{run_id}', 'capability': '', 'prompt_name': '', 'action_json': '{action_json}',
                         'source_record': '{plan_record}'},
                outputs={'act_success': 'success', 'act_record': 'record_path'}, success='success'),
}


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

    A string that is exactly one field, such as "{run_id}", becomes the
    value itself (an empty string when the value is missing, because ROS
    string fields cannot be null), so numbers and lists keep their type.
    Any other string is formatted as text. Mappings and lists are filled
    element by element; numbers, booleans and None are taken literally.
    """
    if isinstance(template, str):
        single = re.fullmatch(r'\{(\w+)\}', template)
        if single:
            value = values.get(single.group(1))
            return '' if value is None else value
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
    """A stage marked pending in the layout: it is reported, never called."""

    def __init__(self, stage, reason=None):
        """Remember the stage and why it is pending."""
        self.stage = stage
        self.reason = reason or f'The {STAGE_TITLES[stage]} stage is marked pending in the layout file.'

    def describe(self):
        """Describe the adapter for the API: it is pending and says why."""
        return dict(stage=self.stage, adapter='pending', configured=False, detail=self.reason)

    async def invoke(self, clients, values, on_event):
        """Refuse to run: a pending stage has no interface to call."""
        raise RosCallFailed('pending', self.reason)


class RosStage:
    """A stage reached through a ROS 2 service or action."""

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
            raise RosCallFailed('failed', f'{STAGE_TITLES[self.stage]} reported that it did not succeed'
                                + (f': {reason}' if reason else ''), result=answer)
        return {name: field(answer, path) for name, path in self.outputs.items()}


def stage_adapter(stage, entry):
    """Build the adapter of one stage from its layout entry, checking every part.

    A missing entry gives SMPA's merged contract for the stage. "pending"
    gives a ``PendingStage``. A mapping is merged over SMPA's contract and
    describes a ``RosStage``: ``kind`` (service or action), ``endpoint`` (an
    absolute ROS name), ``type`` (pkg/srv/Name or pkg/action/Name),
    ``request`` (a template using only values available before this stage),
    ``outputs`` (only this stage's output names), and optional ``success``,
    ``error`` and ``timeout_s``. Anything else is a ``ConfigurationError``.
    """
    if entry == 'pending':
        return PendingStage(stage)
    if entry is not None and not isinstance(entry, dict):
        raise ConfigurationError(f'dialogue.stages.{stage} must be "pending" or a mapping')
    allowed = {'kind', 'endpoint', 'type', 'request', 'outputs', 'success', 'error', 'timeout_s'}
    unknown = set(entry or {}) - allowed
    if unknown:
        raise ConfigurationError(f'dialogue.stages.{stage}: unknown entries {", ".join(sorted(unknown))}')
    entry = dict(SMPA_STAGES[stage], **(entry or {}))
    kind = entry.get('kind')
    if kind not in ('service', 'action'):
        raise ConfigurationError(f'dialogue.stages.{stage}.kind must be "service" or "action"')
    endpoint, type_name = str(entry.get('endpoint') or ''), str(entry.get('type') or '')
    if not NAME.match(endpoint):
        raise ConfigurationError(f'dialogue.stages.{stage}.endpoint must be an absolute ROS name such as /smpa/{stage}')
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
    if not 0.1 <= timeout <= 7200:
        raise ConfigurationError(f'dialogue.stages.{stage}.timeout_s must be between 0.1 and 7200')
    return RosStage(stage, kind, endpoint, type_name, request, {key: str(value) for key, value in outputs.items()},
                    success=entry.get('success'), error=entry.get('error'), timeout=timeout)


def configuration(section):
    """Turn the layout file's ``dialogue`` section into the context mode, Act confirmation and stage adapters.

    Without a section every stage uses SMPA's merged contract, the context
    is independent and Act needs a local confirmation.
    """
    section = section or {}
    if not isinstance(section, dict) or set(section) - {'context', 'stages', 'confirm_act'}:
        raise ConfigurationError('dialogue accepts only: context, stages, confirm_act')
    mode = section.get('context', 'independent')
    if mode not in CONTEXT_MODES:
        raise ConfigurationError('dialogue.context must be "shared" or "independent"')
    confirm = section.get('confirm_act', True)
    if not isinstance(confirm, bool):
        raise ConfigurationError('dialogue.confirm_act must be true or false')
    stages = section.get('stages') or {}
    if not isinstance(stages, dict) or set(stages) - set(STAGES):
        raise ConfigurationError(f'dialogue.stages accepts only: {", ".join(STAGES)}')
    return dict(context=mode, confirm_act=confirm,
                adapters={stage: stage_adapter(stage, stages.get(stage)) for stage in STAGES})


class DialogueCoordinator:
    """Run cognitive steps stage by stage over ROS 2 and publish what happens."""

    def __init__(self, clients, adapters, context_mode, run_reader, robot_run, conversation_session, confirm_act=True):
        """Prepare a coordinator with nothing running.

        ``clients`` makes the ROS calls (``RosClients``); ``adapters`` maps
        each stage to its adapter. ``run_reader`` reads persisted run data
        (an object with ``belief(run_id)``, ``latest_snapshot(run_id)``,
        ``last_sequence(run_id)`` and ``failure_after(run_id, stage,
        sequence)``). ``robot_run()`` is a coroutine returning the robot's
        current run id or None (from SMPA's run control).
        ``conversation_session()`` returns the Dialogue's conversation
        session id or None. ``confirm_act`` decides whether Act waits for a
        local user's approval.
        """
        self.clients = clients
        self.adapters = adapters
        self.runs = run_reader
        self.robot_run = robot_run
        self.conversation_session = conversation_session
        self.confirm_act = confirm_act
        self.context = dict(mode=context_mode, run_id=None, session_id=None)
        self.turns = {}
        self.order = []
        self.requests = {}
        self.active = None
        self.task = None
        self.decisions = {}
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
        """Describe the Dialogue backend: stages, whether steps can run in this context, the context and turns.

        ``path`` says what answers a typed message: SMPA's conversation server
        (always). ``steps`` says whether cognitive steps over the stage
        actions are possible: ``available`` in the shared context with no
        stage pending, otherwise the reason.
        """
        pending = [stage for stage in STAGES if isinstance(self.adapters[stage], PendingStage)]
        if pending:
            steps = 'pending_stages'
        elif self.context['mode'] != 'shared':
            steps = 'independent_context'
        else:
            steps = 'available'
        return dict(path='conversation_server', ros_path_ready=not pending, steps=steps, pending_stages=pending,
                    confirm_act=self.confirm_act, stages=self.stages(), context=dict(self.context),
                    active_turn=self.active, turns=[self.summary(self.turns[turn_id]) for turn_id in self.order[-20:]])

    def summary(self, turn):
        """Return a copy of a turn record that callers may keep."""
        return json.loads(json.dumps(turn, default=str))

    def get(self, turn_id):
        """Return one turn record, or raise a 404 ``DialogueError``."""
        if turn_id not in self.turns:
            raise DialogueError(404, 'Unknown turn')
        return self.summary(self.turns[turn_id])

    # ----- context ------------------------------------------------------------------------------

    def choose_context(self, mode, run_id=None, session_id=None):
        """Choose the shared or independent context, optionally naming the robot run and the Dialogue session.

        In the shared context ``run_id`` names the robot's run to use; left
        out, the run SMPA's run control reports as running is used at each
        step. ``session_id`` names the Dialogue's conversation session; left
        out, the conversation the GUI currently holds. A running turn keeps
        the context it started with.
        """
        if mode not in CONTEXT_MODES:
            raise DialogueError(422, 'The context must be "shared" or "independent"')
        self.context = dict(mode=mode, run_id=run_id or None, session_id=session_id or None)
        return dict(self.context)

    async def resolve_context(self):
        """Work out the identifiers a step carries, or explain why no step can run.

        Independent: the stage actions need a robot run; the Dialogue's own
        beliefs are kept by the conversation server, so a step is refused
        (409). Shared: the robot run is the chosen run or the one SMPA's run
        control reports as running; without one the step is refused (409).
        The belief context is the robot run; the previous snapshot is the
        latest one the run's Model produced.
        """
        session = self.context['session_id'] or self.conversation_session()
        if self.context['mode'] != 'shared':
            raise DialogueError(409, 'Cognitive steps call SMPA\'s stage actions on a running robot run. In the '
                                     'independent context the Dialogue keeps its own beliefs through the conversation '
                                     'server; choose the shared context to step the robot\'s run.')
        run = self.context['run_id'] or await self.robot_run()
        if not run:
            raise DialogueError(409, 'The shared context needs the robot\'s run: SMPA\'s run control reports no '
                                     'running run, and none was chosen. Start a robot run first.')
        try:
            previous = self.runs.latest_snapshot(run)
        except Exception:  # noqa: BLE001 - an unreadable run is reported by the stage itself
            previous = ''
        return dict(context_mode='shared', run_id=run, session_id=session, belief_context_id=run,
                    previous_snapshot=previous)

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
        values = dict(values, message=message or '', turn_id=turn_id)
        turn = dict(turn_id=turn_id, message=message or '', state='running', created_at=time.time(), finished_at=None,
                    retry_of=retry_of, failed_stage=None, error=None, values=values, snapshot=None, belief=None,
                    belief_before=None, act_decision=None,
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
            raise DialogueError(409, 'A cognitive step is still running; wait for it to finish')
        return None

    async def submit(self, message, request_id):
        """Start one cognitive step on the robot's run and return its record at once.

        The step runs in the background; its progress is published as events
        and kept in the record. It is refused (409) while a stage is pending,
        while another step runs, or when the context has no robot run. A
        repeated request id returns the first record. ``message`` is an
        optional note kept with the record (typed messages themselves go
        through the conversation server, where a robot that reads this
        conversation picks them up in its next Sense).
        """
        existing = self.check_free(request_id)
        if existing:
            return existing
        if not self.ros_path_ready():
            pending = ', '.join(stage for stage in STAGES if isinstance(self.adapters[stage], PendingStage))
            raise DialogueError(409, f'Cognitive steps are not configured: stages marked pending: {pending}.')
        values = await self.resolve_context()
        turn = self.new_turn(message, values)
        self.requests[request_id] = turn['turn_id']
        self.start(turn, 'sense')
        return self.summary(turn)

    async def retry(self, turn_id, request_id):
        """Run a failed turn again from the stage that failed, reusing what the earlier stages produced.

        Stages that succeeded are not called again, so a retry neither
        repeats the Sense observation nor publishes a second snapshot for the
        same step. The new turn keeps the original identifiers and refers to
        the failed one with ``retry_of``.
        """
        existing = self.check_free(request_id)
        if existing:
            return existing
        failed = self.turns.get(turn_id)
        if failed is None:
            raise DialogueError(404, 'Unknown turn')
        if failed['state'] != 'failed' or failed['failed_stage'] is None:
            raise DialogueError(409, 'Only a turn that failed at a stage can be retried')
        values = {key: value for key, value in failed['values'].items() if key not in ('message', 'turn_id')}
        turn = self.new_turn(failed['message'], values, start_stage=failed['failed_stage'], retry_of=turn_id)
        for stage in STAGES[:STAGES.index(failed['failed_stage'])]:
            turn['stages'][stage]['outputs'] = dict(failed['stages'][stage]['outputs'])
        turn['snapshot'], turn['belief'] = failed['snapshot'], failed['belief']
        self.requests[request_id] = turn['turn_id']
        self.start(turn, failed['failed_stage'])
        return self.summary(turn)

    def decide(self, turn_id, approve):
        """Record a local user's decision about the Act step a turn is waiting for.

        Only a turn that waits for this decision accepts it. Approval lets
        the turn call Act with the plan's decision; declining ends the turn
        as ``declined`` without calling Act. Returns the turn record.
        """
        turn = self.turns.get(turn_id)
        if turn is None:
            raise DialogueError(404, 'Unknown turn')
        waiting = self.decisions.get(turn_id)
        if waiting is None or waiting.done():
            raise DialogueError(409, 'This turn is not waiting for an Act decision')
        waiting.set_result(bool(approve))
        return self.summary(turn)

    def start(self, turn, first):
        """Mark the turn active and run it as a background task of the event loop."""
        self.active = turn['turn_id']
        self.task = asyncio.get_running_loop().create_task(self.execute(turn, first))

    async def confirmation(self, turn):
        """Wait for the local user's Act decision; return whether Act may run.

        The turn's state becomes ``awaiting_act`` with the plan's decision
        (``act_decision``), and an ``act_confirmation_required`` event is
        published. Afterwards the state is ``running`` again.
        """
        values = turn['values']
        try:
            decision = json.loads(values.get('action_json') or 'null')
        except ValueError:
            decision = values.get('action_json')
        turn.update(state='awaiting_act', act_decision=decision)
        waiting = asyncio.get_running_loop().create_future()
        self.decisions[turn['turn_id']] = waiting
        self.publish('act_confirmation_required', turn_id=turn['turn_id'], run_id=values.get('run_id'),
                     action=decision, plan_record=values.get('plan_record'))
        try:
            approved = await waiting
        finally:
            self.decisions.pop(turn['turn_id'], None)
        turn['state'] = 'running'
        self.publish('act_decision', turn_id=turn['turn_id'], approved=approved)
        return approved

    def explain(self, values, stage, before, error):
        """Add the reason SMPA persisted for an aborted stage to the error, when its record names one."""
        if error.state != 'aborted':
            return str(error)
        try:
            reason = self.runs.failure_after(values['run_id'], stage, before)
        except Exception:  # noqa: BLE001 - the abort itself is still reported
            reason = None
        return f'{error} — recorded reason: {reason}' if reason else str(error)

    async def execute(self, turn, first):
        """Run the stages from ``first`` to Act, one after the other, and record and publish each step.

        Each stage is called through its adapter with the turn values; its
        outputs become turn values for the later stages. After Model succeeds
        the world-model snapshot it reported and the run's belief graph are
        read and published. Before Act, a local user's approval is awaited
        when confirmation is on; declining ends the turn as ``declined``. The
        first failing stage ends the turn as failed, with the reason SMPA
        recorded when the stage aborted; later stages are not called.
        Whatever happens, the coordinator is free for the next turn afterwards.
        """
        values = turn['values']
        self.publish('turn_started', turn_id=turn['turn_id'], message=turn['message'], retry_of=turn['retry_of'],
                     context={key: values.get(key) for key in CONTEXT_VALUES[2:]})
        try:
            for stage in STAGES[STAGES.index(first):]:
                if stage == 'model':
                    turn['belief_before'] = self.snapshot(values['belief_context_id'])
                if stage == 'act' and self.confirm_act and not await self.confirmation(turn):
                    turn['stages']['act'].update(state='declined', finished_at=time.time())
                    turn.update(state='declined', finished_at=time.time())
                    self.publish('turn_declined', turn_id=turn['turn_id'])
                    return
                record = turn['stages'][stage]
                record.update(state='running', started_at=time.time())
                self.publish('stage_started', turn_id=turn['turn_id'], stage=stage)
                before = self.sequence(values['run_id'])

                def forward(kind, details, stage=stage):
                    """Publish an action's acceptance or feedback as a stage event."""
                    self.publish(f'stage_{kind}', turn_id=turn['turn_id'], stage=stage, **details)
                try:
                    outputs = await self.adapters[stage].invoke(self.clients, values, forward)
                    missing = [name for name in REQUIRED_OUTPUTS.get(stage, ()) if outputs.get(name) in (None, '')]
                    if missing:
                        raise RosCallFailed('failed', f'{STAGE_TITLES[stage]} returned no {", ".join(missing)}')
                except RosCallFailed as error:
                    message = self.explain(values, stage, before, error)
                    record.update(state=error.state, finished_at=time.time(), error=message)
                    turn.update(state='failed', failed_stage=stage, error=message, finished_at=time.time())
                    self.publish('stage_failed', turn_id=turn['turn_id'], stage=stage, state=error.state, error=message)
                    self.publish('turn_failed', turn_id=turn['turn_id'], stage=stage, error=message)
                    return
                record.update(state='succeeded', finished_at=time.time(), outputs=outputs)
                values.update({name: value for name, value in outputs.items() if value is not None})
                self.publish('stage_completed', turn_id=turn['turn_id'], stage=stage, outputs=outputs)
                if stage == 'model':
                    after = self.snapshot(values['belief_context_id'])
                    before_belief = turn['belief_before'] or {}
                    turn['belief'] = dict(after, changed=after.get('revision') != before_belief.get('revision'),
                                          model_record=values.get('model_record'))
                    turn['snapshot'] = dict(path=values.get('snapshot_path') or None, model_record=values.get('model_record'))
                    values['previous_snapshot'] = values.get('snapshot_path') or values.get('previous_snapshot')
                    self.publish('model_snapshot', turn_id=turn['turn_id'], snapshot=turn['snapshot'], belief=turn['belief'])
            turn.update(state='completed', finished_at=time.time())
            self.publish('turn_completed', turn_id=turn['turn_id'], **{name: values.get(name) for name in (
                'run_id', 'sense_record', 'model_record', 'snapshot_path', 'plan_record', 'action_json', 'act_record',
                'act_success')})
        except Exception as error:  # noqa: BLE001 - an unexpected failure must still free the coordinator
            turn.update(state='failed', error=f'Unexpected error: {error}', finished_at=time.time())
            self.publish('turn_failed', turn_id=turn['turn_id'], error=turn['error'])
        finally:
            self.active = None

    def sequence(self, run_id):
        """Return the run's highest record sequence, or 0 when it cannot be read."""
        try:
            return self.runs.last_sequence(run_id)
        except Exception:  # noqa: BLE001 - only used to find a later failure record
            return 0

    def snapshot(self, belief_context_id):
        """Read the belief graph of a run: its run, file, state, revision and triple count."""
        try:
            reading = self.runs.belief(belief_context_id)
        except Exception as error:  # noqa: BLE001 - a missing run is a state of the snapshot
            return dict(belief_context_id=belief_context_id, state='unavailable', detail=str(error), revision=None)
        return dict(belief_context_id=belief_context_id, state=reading.get('state'), path=reading.get('path'),
                    revision=reading.get('revision'), triples=reading.get('triples'))

    async def shutdown(self):
        """Stop a running turn when the GUI stops; its record says it was interrupted. Act is never started then."""
        if self.task is not None and not self.task.done():
            turn = self.turns.get(self.active) if self.active else None
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - ending anyway
                pass
            if turn is not None and turn['state'] in ('running', 'awaiting_act'):
                turn.update(state='interrupted', finished_at=time.time())
        self.active = None
