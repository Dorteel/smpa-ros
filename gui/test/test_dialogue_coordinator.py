"""Unit tests of the Dialogue coordinator and its configuration, without ROS.

The stage adapters here are fakes that record what they were given; the
ROS calls themselves are tested in test_ros_clients.py and
test_dialogue_ros.py.
"""
import asyncio

import pytest

from smpa_gui.ros_adapter.ros_clients import RosCallFailed
from smpa_gui.smpa_adapter import dialogue


class FakeStage:
    """A stage that records its calls and returns fixed outputs, or fails once, or waits."""

    def __init__(self, stage, outputs=None, failure=None, delay=0.0, log=None):
        """Remember what to return or how to fail, and where to log calls."""
        self.stage, self.outputs, self.failure, self.delay, self.log = stage, outputs or {}, failure, delay, log

    def describe(self):
        """Describe the fake as a configured adapter."""
        return dict(stage=self.stage, adapter='fake', configured=True)

    async def invoke(self, clients, values, on_event):
        """Record the call with a copy of the values, then return, fail or wait as configured."""
        self.log.append((self.stage, dict(values)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure
        return dict(self.outputs)


class FakeRuns:
    """Persisted run data as the coordinator reads it: belief revisions, snapshots, sequences and failures."""

    def __init__(self):
        """Start with one belief revision and no snapshot."""
        self.revision, self.latest, self.reason = 'rev-a', 'world_model/iteration_000.md', None

    def belief(self, run_id):
        """Return the current fake belief reading."""
        return dict(state='ok', path=f'/memory/{run_id}/belief-state.ttl', revision=self.revision, triples=1)

    def latest_snapshot(self, run_id):
        """Return the latest snapshot path."""
        return self.latest

    def last_sequence(self, run_id):
        """Return a fixed sequence number."""
        return 7

    def failure_after(self, run_id, stage, sequence):
        """Return the scripted recorded reason."""
        return self.reason


def stages(log, **overrides):
    """Return four fake stages with SMPA-shaped outputs, some replaced."""
    made = dict(sense=FakeStage('sense', dict(observation='{"vision": "x"}', sense_record='state/cycle_000001/sense.json'), log=log),
                model=FakeStage('model', dict(world_model='# W', model_record='state/cycle_000001/model.json',
                                              snapshot_path='world_model/iteration_001.md'), log=log),
                plan=FakeStage('plan', dict(action_json='{"action": "turn_left"}', plan_record='state/cycle_000001/plan.json'), log=log),
                act=FakeStage('act', dict(act_success=True, act_record='state/cycle_000001/act.json'), log=log))
    made.update(overrides)
    return made


def coordinator(log, runs=None, confirm=False, **overrides):
    """Build a coordinator in the shared context with fake stages, runs and a robot run."""
    async def robot_run():
        """Report the robot's run id."""
        return 'robot-run'
    target = dialogue.DialogueCoordinator(None, stages(log, **overrides), 'shared', runs or FakeRuns(), robot_run,
                                          lambda: 'dialogue-session', confirm_act=confirm)
    return target


async def run_turn(target, note='', request_id='request-0001'):
    """Submit a step and wait for its background task; return the final record."""
    record = await target.submit(note, request_id)
    await target.task
    return target.get(record['turn_id'])


def test_configuration_defaults_to_smpas_contracts_and_checks_every_entry():
    """Without a section the four stages use /smpa/<stage> with SMPA's types; malformed entries are refused."""
    settings = dialogue.configuration(None)
    assert settings['context'] == 'independent' and settings['confirm_act'] is True
    described = {stage: adapter.describe() for stage, adapter in settings['adapters'].items()}
    assert {stage: (item['endpoint'], item['type']) for stage, item in described.items()} == {
        stage: (f'/smpa/{stage}', f'smpa_ros_interfaces/action/{stage.title()}') for stage in dialogue.STAGES}
    assert described['model']['request']['previous_snapshot'] == '{previous_snapshot}'
    renamed = dialogue.configuration(dict(stages=dict(plan=dict(endpoint='/robot/smpa/plan'), act='pending'), confirm_act=False))
    assert renamed['adapters']['plan'].endpoint == '/robot/smpa/plan' and renamed['adapters']['plan'].type_name.endswith('/Plan')
    assert isinstance(renamed['adapters']['act'], dialogue.PendingStage) and renamed['confirm_act'] is False
    problems = {
        'dialogue.context': dict(context='both'),
        'confirm_act must be true or false': dict(confirm_act='yes'),
        'dialogue.stages accepts only': dict(stages=dict(think='pending')),
        'kind must be': dict(stages=dict(plan=dict(kind='topic'))),
        'endpoint must be an absolute ROS name': dict(stages=dict(plan=dict(endpoint='plan'))),
        'type must look like package/action/Name': dict(stages=dict(plan=dict(type='pkg/srv/Plan'))),
        'uses values not available before sense': dict(stages=dict(sense=dict(request={'x': '{world_model}'}))),
        'outputs may name only': dict(stages=dict(plan=dict(outputs={'observation': 'x'}))),
        'must say where to find action_json': dict(stages=dict(plan=dict(outputs={'plan_record': 'record_path'}))),
        'unknown entries': dict(stages=dict(plan=dict(colour='red'))),
    }
    for fragment, section in problems.items():
        with pytest.raises(dialogue.ConfigurationError) as refused:
            dialogue.configuration(section)
        assert fragment in str(refused.value), (fragment, str(refused.value))


def test_templates_fill_requests_and_outputs_read_dotted_fields():
    """A lone field keeps its value (missing becomes ''), mixed text is formatted; outputs read dotted paths."""
    values = dict(message='hi', count=3, run_id=None)
    assert dialogue.render({'a': '{count}', 'b': 'say {message}!', 'c': ['{message}', 7], 'd': {'e': '{run_id}'}},
                           values) == {'a': 3, 'b': 'say hi!', 'c': ['hi', 7], 'd': {'e': ''}}
    assert dialogue.field(dict(reply=dict(text='x')), 'reply.text') == 'x' and dialogue.field({}, 'reply.text') is None


def test_step_calls_stages_in_order_and_carries_smpa_values_forward():
    """Each stage gets the run, the latest snapshot and every earlier output; the record keeps them associated."""
    log, runs = [], FakeRuns()

    async def scenario():
        """Run one step with the Model fake changing the belief revision."""
        target = coordinator(log, runs)
        original = target.adapters['model'].invoke

        async def model_commits(clients, values, on_event):
            """Pretend Model committed a new belief revision."""
            runs.revision = 'rev-b'
            return await original(clients, values, on_event)
        target.adapters['model'].invoke = model_commits
        events = target.listen()
        record = await run_turn(target, 'a note')
        return record, [events.get_nowait() for _ in range(events.qsize())]
    record, events = asyncio.run(scenario())
    assert [stage for stage, _ in log] == ['sense', 'model', 'plan', 'act']
    assert log[0][1]['run_id'] == 'robot-run' and log[0][1]['previous_snapshot'] == 'world_model/iteration_000.md'
    assert log[1][1]['sense_record'] == 'state/cycle_000001/sense.json' and 'model_record' not in log[1][1]
    assert log[3][1]['action_json'] == '{"action": "turn_left"}' and log[3][1]['plan_record'].endswith('plan.json')
    assert record['state'] == 'completed' and record['belief']['changed'] is True and record['message'] == 'a note'
    assert record['snapshot'] == dict(path='world_model/iteration_001.md', model_record='state/cycle_000001/model.json')
    names = [event['event'] for event in events]
    model_done = next(index for index, event in enumerate(events) if event['event'] == 'stage_completed' and event['stage'] == 'model')
    assert names[0] == 'turn_started' and names[-1] == 'turn_completed' and names.index('model_snapshot') == model_done + 1
    assert events[-1]['act_record'].endswith('act.json') and events[-1]['act_success'] is True


def test_act_waits_for_a_decision_and_declining_calls_nothing():
    """With confirmation on, the step waits before Act; approval runs Act, declining ends the step without it."""
    async def scenario(approve):
        """Run a step, decide when it waits, and return the record and the logged stages."""
        log = []
        target = coordinator(log, confirm=True)
        record = await target.submit('', f'request-{approve}')
        while target.turns[record['turn_id']]['state'] != 'awaiting_act':
            await asyncio.sleep(0.01)
        waiting = target.get(record['turn_id'])
        with pytest.raises(dialogue.DialogueError):
            target.decide('unknown', True)
        target.decide(record['turn_id'], approve)
        await target.task
        with pytest.raises(dialogue.DialogueError) as again:
            target.decide(record['turn_id'], approve)
        return waiting, target.get(record['turn_id']), [stage for stage, _ in log], again.value.status
    waiting, approved, called, status = asyncio.run(scenario(True))
    assert waiting['act_decision'] == {'action': 'turn_left'} and approved['state'] == 'completed'
    assert called == ['sense', 'model', 'plan', 'act'] and status == 409
    _, declined, called, _ = asyncio.run(scenario(False))
    assert declined['state'] == 'declined' and declined['stages']['act']['state'] == 'declined'
    assert called == ['sense', 'model', 'plan']


def test_aborted_stage_is_explained_by_its_record_and_retry_resumes():
    """An aborted Model is reported with SMPA's recorded reason; the retry starts at Model with Sense's output."""
    log, runs = [], FakeRuns()
    runs.reason = 'ValueError: Model source and previous snapshot must exist within the run'

    async def scenario():
        """Fail Model once, then retry."""
        target = coordinator(log, runs, model=FakeStage('model', dict(world_model='# W', model_record='m.json'),
                                                        failure=RosCallFailed('aborted', 'The action server ended the goal as aborted'), log=log))
        failed = await run_turn(target)
        retried = await target.retry(failed['turn_id'], 'request-0002')
        await target.task
        return failed, target.get(retried['turn_id'])
    failed, retried = asyncio.run(scenario())
    assert failed['failed_stage'] == 'model' and failed['belief'] is None and failed['snapshot'] is None
    assert failed['error'].endswith('recorded reason: ValueError: Model source and previous snapshot must exist within the run')
    assert [stage for stage, _ in log] == ['sense', 'model', 'model', 'plan', 'act']
    assert retried['state'] == 'completed' and retried['retry_of'] == failed['turn_id']
    assert retried['stages']['sense']['state'] == 'skipped'


def test_missing_outputs_unexpected_errors_and_busy_refusals():
    """Plan without action_json fails; an unexpected error still frees the coordinator; one step runs at a time."""
    log = []

    async def scenario():
        """Run the three situations one after the other."""
        silent = coordinator(log, plan=FakeStage('plan', dict(plan_record='p.json'), log=log))
        no_decision = await run_turn(silent)
        broken = coordinator(log, act=FakeStage('act', failure=ZeroDivisionError('boom'), log=log))
        crashed = await run_turn(broken)
        slow = coordinator(log, sense=FakeStage('sense', dict(sense_record='s.json'), delay=0.3, log=log))
        first = await slow.submit('', 'request-0003')
        with pytest.raises(dialogue.DialogueError) as busy:
            await slow.submit('', 'request-0004')
        again = await slow.submit('', 'request-0003')
        await slow.task
        return no_decision, crashed, broken.active, busy.value, first, again
    no_decision, crashed, active, busy, first, again = asyncio.run(scenario())
    assert no_decision['failed_stage'] == 'plan' and 'returned no action_json' in no_decision['error']
    assert crashed['state'] == 'failed' and 'Unexpected error: boom' in crashed['error'] and active is None
    assert busy.status == 409 and again['turn_id'] == first['turn_id'] and again['duplicate'] is True


def test_contexts_resolve_and_refuse_with_reasons():
    """Shared uses the robot's run or a chosen run; independent and a missing run are refused; pending stages too."""
    async def scenario():
        """Resolve each context and try steps that cannot run."""
        target = coordinator([])
        shared = await target.resolve_context()
        target.choose_context('shared', run_id='chosen-run', session_id='talk')
        chosen = await target.resolve_context()
        target.choose_context('independent')
        with pytest.raises(dialogue.DialogueError) as independent:
            await target.resolve_context()
        nobody = coordinator([])
        nobody.robot_run = lambda: asyncio.sleep(0, None)
        with pytest.raises(dialogue.DialogueError) as no_run:
            await nobody.resolve_context()
        pending = dialogue.DialogueCoordinator(None, dialogue.configuration(dict(stages=dict(sense='pending')))['adapters'],
                                               'shared', FakeRuns(), None, lambda: None)
        with pytest.raises(dialogue.DialogueError) as refused:
            await pending.submit('', 'request-0005')
        return shared, chosen, independent.value, no_run.value, refused.value, pending.status(), target.status()
    shared, chosen, independent, no_run, refused, pending_status, status = asyncio.run(scenario())
    assert shared == dict(context_mode='shared', run_id='robot-run', session_id='dialogue-session',
                          belief_context_id='robot-run', previous_snapshot='world_model/iteration_000.md')
    assert chosen['run_id'] == 'chosen-run' and chosen['session_id'] == 'talk'
    assert independent.status == 409 and 'independent context' in str(independent)
    assert no_run.status == 409 and refused.status == 409 and 'sense' in str(refused)
    assert pending_status['steps'] == 'pending_stages' and status['steps'] == 'independent_context'
    with pytest.raises(dialogue.DialogueError):
        coordinator([]).choose_context('everything')


def test_shutdown_marks_a_waiting_step_interrupted_without_acting():
    """Stopping the GUI while a step waits for the Act decision cancels it; Act is never called."""
    async def scenario():
        """Start a step that waits for approval and shut down."""
        log = []
        target = coordinator(log, confirm=True)
        record = await target.submit('', 'request-0006')
        while target.turns[record['turn_id']]['state'] != 'awaiting_act':
            await asyncio.sleep(0.01)
        await target.shutdown()
        return target.get(record['turn_id']), target.active, [stage for stage, _ in log]
    record, active, called = asyncio.run(scenario())
    assert record['state'] == 'interrupted' and active is None and 'act' not in called
