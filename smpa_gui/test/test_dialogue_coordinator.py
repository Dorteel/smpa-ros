"""Unit tests of the Dialogue coordinator and its configuration, without ROS.

The stage adapters here are fakes that record what they were given; the
ROS calls themselves are tested in test_ros_clients.py and
test_dialogue_ros.py.
"""
import asyncio

import pytest

from smpa_gui.ros_adapter.ros_clients import RosCallFailed
from smpa_gui.smpa_adapter import dialogue

GOOD_STAGE = dict(kind='action', endpoint='/dialogue/plan', type='pkg/action/Plan',
                  request={'text': '{message}', 'context': {'run': '{run_id}'}}, outputs={'response': 'reply.text'})


class FakeStage:
    """A stage that records its calls and returns fixed outputs, or fails, or waits."""

    def __init__(self, stage, outputs=None, failure=None, delay=0.0, log=None):
        """Remember what to return or how to fail, and where to log calls."""
        self.stage, self.outputs, self.failure, self.delay, self.log = stage, outputs or {}, failure, delay, log

    def describe(self):
        """Describe the fake as a configured adapter."""
        return dict(stage=self.stage, adapter='fake', configured=True)

    async def invoke(self, clients, values, on_event):
        """Record the call with a copy of the values, then return, fail or wait as configured."""
        self.log.append((self.stage, dict(values)))
        on_event('feedback', dict(feedback=dict(note=self.stage)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            failure, self.failure = self.failure, None  # fail once
            raise failure
        return dict(self.outputs)


def coordinator(log, beliefs, **overrides):
    """Build a coordinator with fake stages, a fake belief reader and a fake robot run."""
    stages = dict(sense=FakeStage('sense', dict(observation_id='obs-1'), log=log),
                  model=FakeStage('model', dict(model_invocation_id='inv-1'), log=log),
                  plan=FakeStage('plan', dict(response='hello back'), log=log),
                  act=FakeStage('act', dict(delivery='spoken'), log=log))
    stages.update(overrides)

    def read_belief(run_id):
        """Return the current fake belief revision of a run."""
        return dict(state='ok', path=f'/memory/{run_id}/belief-state.ttl', revision=beliefs.get(run_id), triples=1)

    async def robot_run():
        """Report the robot's run id."""
        return 'robot-run'
    return dialogue.DialogueCoordinator(None, stages, 'independent', read_belief, robot_run, lambda: 'my-session')


async def run_turn(target, message='hi', request_id='request-0001'):
    """Submit a turn and wait for its background task; return the final record."""
    record = await target.submit(message, request_id)
    await target.task
    return target.get(record['turn_id'])


def test_configuration_defaults_to_pending_and_checks_every_entry():
    """Without a section every stage is pending; malformed stage entries are refused with their path."""
    settings = dialogue.configuration(None)
    assert settings['context'] == 'independent'
    assert all(isinstance(adapter, dialogue.PendingStage) for adapter in settings['adapters'].values())
    configured = dialogue.configuration(dict(context='shared', stages=dict(plan=GOOD_STAGE)))
    assert configured['context'] == 'shared' and isinstance(configured['adapters']['plan'], dialogue.RosStage)
    problems = {
        'dialogue.context': dict(context='both'),
        'dialogue.stages accepts only': dict(stages=dict(think='pending')),
        'kind must be': dict(stages=dict(plan=dict(GOOD_STAGE, kind='topic'))),
        'endpoint must be an absolute ROS name': dict(stages=dict(plan=dict(GOOD_STAGE, endpoint='plan'))),
        'type must look like package/action/Name': dict(stages=dict(plan=dict(GOOD_STAGE, type='pkg/srv/Plan'))),
        'uses values not available before sense': dict(stages=dict(sense=dict(GOOD_STAGE, outputs={}, request={'x': '{response}'}))),
        'outputs may name only': dict(stages=dict(plan=dict(GOOD_STAGE, outputs={'observation_id': 'x'}))),
        'must say where to find response': dict(stages=dict(plan=dict(GOOD_STAGE, outputs={}))),
        'unknown entries': dict(stages=dict(plan=dict(GOOD_STAGE, colour='red'))),
    }
    for fragment, section in problems.items():
        with pytest.raises(dialogue.ConfigurationError) as refused:
            dialogue.configuration(section)
        assert fragment in str(refused.value), (fragment, str(refused.value))


def test_templates_fill_requests_and_outputs_read_dotted_fields():
    """A lone field keeps its value's type, mixed text is formatted, nesting is followed; outputs read dotted paths."""
    values = dict(message='hi', count=3, run_id=None)
    assert dialogue.render({'a': '{count}', 'b': 'say {message}!', 'c': ['{message}', 7], 'd': {'e': '{run_id}'}},
                           values) == {'a': 3, 'b': 'say hi!', 'c': ['hi', 7], 'd': {'e': None}}
    assert dialogue.field(dict(reply=dict(text='x')), 'reply.text') == 'x' and dialogue.field({}, 'reply.text') is None


def test_turn_calls_stages_in_order_and_carries_values_forward():
    """Each stage gets the context and every earlier output; the record associates message, outputs and snapshot."""
    log, beliefs = [], {'my-session': 'rev-a'}

    async def scenario():
        """Run one turn with the Model fake changing the belief revision."""
        target = coordinator(log, beliefs)
        original = target.adapters['model'].invoke

        async def model_commits(clients, values, on_event):
            """Pretend Model committed a new belief revision."""
            beliefs['my-session'] = 'rev-b'
            return await original(clients, values, on_event)
        target.adapters['model'].invoke = model_commits
        events = target.listen()
        record = await run_turn(target)
        return record, [events.get_nowait() for _ in range(events.qsize())]
    record, events = asyncio.run(scenario())
    assert [stage for stage, _ in log] == ['sense', 'model', 'plan', 'act']
    assert log[0][1]['context_mode'] == 'independent' and log[0][1]['belief_context_id'] == 'my-session'
    assert log[1][1]['observation_id'] == 'obs-1' and 'model_invocation_id' not in log[1][1]
    assert log[3][1]['response'] == 'hello back' and log[3][1]['model_invocation_id'] == 'inv-1'
    assert record['state'] == 'completed' and record['belief']['revision'] == 'rev-b' and record['belief']['changed'] is True
    assert record['belief_before']['revision'] == 'rev-a'
    names = [event['event'] for event in events]
    assert names[0] == 'turn_started' and names[-1] == 'turn_completed'
    model_done = next(index for index, event in enumerate(events)
                      if event['event'] == 'stage_completed' and event['stage'] == 'model')
    assert names.index('belief_snapshot') == model_done + 1  # read only after Model completed
    assert events[-1]['response'] == 'hello back' and events[-1]['belief_revision'] == 'rev-b'


def test_failure_stops_the_turn_publishes_no_snapshot_and_retry_resumes():
    """A failing Model ends the turn before Plan without a snapshot; the retry starts at Model with Sense's output."""
    log, beliefs = [], {'my-session': 'rev-a'}

    async def scenario():
        """Fail Model once, then retry."""
        target = coordinator(log, beliefs, model=FakeStage('model', dict(model_invocation_id='inv-2'),
                                                           failure=RosCallFailed('aborted', 'Model gave up'), log=log))
        failed = await run_turn(target)
        retried = await target.retry(failed['turn_id'], 'request-0002')
        await target.task
        return failed, target.get(retried['turn_id'])
    failed, retried = asyncio.run(scenario())
    assert failed['state'] == 'failed' and failed['failed_stage'] == 'model' and failed['belief'] is None
    assert failed['stages']['model'] == dict(failed['stages']['model'], state='aborted', error='Model gave up')
    assert failed['stages']['plan']['state'] == 'waiting'
    assert [stage for stage, _ in log] == ['sense', 'model', 'model', 'plan', 'act']
    assert log[2][1]['observation_id'] == 'obs-1'
    assert retried['state'] == 'completed' and retried['retry_of'] == failed['turn_id']
    assert retried['stages']['sense'] == dict(retried['stages']['sense'], state='skipped', outputs={'observation_id': 'obs-1'})


def test_missing_response_unexpected_errors_and_busy_refusals():
    """Plan without a response fails; an unexpected error still frees the coordinator; one turn runs at a time."""
    log = []

    async def scenario():
        """Run the three situations one after the other."""
        silent = coordinator(log, {}, plan=FakeStage('plan', {}, log=log))
        no_reply = await run_turn(silent)
        broken = coordinator(log, {}, act=FakeStage('act', failure=ZeroDivisionError('boom'), log=log))
        crashed = await run_turn(broken)
        slow = coordinator(log, {}, sense=FakeStage('sense', dict(observation_id='o'), delay=0.3, log=log))
        first = await slow.submit('one', 'request-0003')
        with pytest.raises(dialogue.DialogueError) as busy:
            await slow.submit('two', 'request-0004')
        again = await slow.submit('one', 'request-0003')
        await slow.task
        return no_reply, crashed, broken.active, busy.value, first, again
    no_reply, crashed, active, busy, first, again = asyncio.run(scenario())
    assert no_reply['failed_stage'] == 'plan' and 'returned no response' in no_reply['error']
    assert crashed['state'] == 'failed' and 'Unexpected error: boom' in crashed['error'] and active is None
    assert busy.status == 409 and again['turn_id'] == first['turn_id'] and again['duplicate'] is True


def test_contexts_resolve_to_the_right_identifiers_and_pending_stages_refuse():
    """Shared uses the robot's run or a chosen run; independent uses the session; pending stages refuse turns."""
    async def scenario():
        """Resolve each context and try a turn with pending stages."""
        target = coordinator([], {})
        independent = await target.resolve_context()
        target.choose_context('shared')
        shared = await target.resolve_context()
        target.choose_context('shared', run_id='chosen-run')
        chosen = await target.resolve_context()
        target.choose_context('independent', session_id='other-session')
        other = await target.resolve_context()
        nobody = coordinator([], {})
        nobody.robot_run = lambda: asyncio.sleep(0, None)
        nobody.choose_context('shared')
        with pytest.raises(dialogue.DialogueError) as no_run:
            await nobody.resolve_context()
        pending = dialogue.DialogueCoordinator(None, dialogue.configuration(None)['adapters'], 'independent',
                                               lambda run: {}, None, lambda: 'x')
        with pytest.raises(dialogue.DialogueError) as refused:
            await pending.submit('hi', 'request-0005')
        return independent, shared, chosen, other, no_run.value, refused.value, pending.status()
    independent, shared, chosen, other, no_run, refused, status = asyncio.run(scenario())
    assert independent == dict(context_mode='independent', run_id='my-session', session_id='my-session',
                               belief_context_id='my-session')
    assert shared == dict(context_mode='shared', run_id='robot-run', session_id='my-session', belief_context_id='robot-run')
    assert chosen['belief_context_id'] == 'chosen-run' and other['belief_context_id'] == 'other-session'
    assert no_run.status == 409
    assert refused.status == 409 and 'sense, model, plan, act' in str(refused)
    assert status['path'] == 'conversation_server' and status['ros_path_ready'] is False
    with pytest.raises(dialogue.DialogueError):
        coordinator([], {}).choose_context('everything')


def test_shutdown_marks_a_running_turn_interrupted():
    """Stopping the GUI during a turn cancels it and records it as interrupted."""
    async def scenario():
        """Start a slow turn and shut down while it runs."""
        target = coordinator([], {}, sense=FakeStage('sense', delay=5, log=[]))
        record = await target.submit('hi', 'request-0006')
        await asyncio.sleep(0.05)
        await target.shutdown()
        return target.get(record['turn_id']), target.active
    record, active = asyncio.run(scenario())
    assert record['state'] == 'interrupted' and active is None
