"""Exercise Read consumption using real conversation and robot persistence."""
from functools import partial
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from memory import MemoryManager
from sense import modalities
from sense.read import observe
from smpa_ros.conversation import exchange, open_conversation, append_message
from smpa_ros.sense import record_observation


def reply(history, **settings):
    """Return a deterministic reply without loading a conversational model."""
    return 'Acknowledged.'


def collect(directory, cycle, *, session):
    """Propose one attributed Read batch without committing its cursor."""
    return json.dumps({'read': observe(directory, session)})


@pytest.fixture
def sources(tmp_path):
    """Create independent source and robot runs with Read as the only modality."""
    source = open_conversation(tmp_path / 'conversations')
    robot = MemoryManager(tmp_path / 'robots', 'test', goal='Read user statements.')
    config = {'sensing': {'hear': False, 'vision': False,
                         'read': {'enabled': True, 'session': str(source.directory)}}}
    return robot, source, config


def persist(robot, source, config, cycle=1):
    """Commit a Read batch through the production robot Sense record writer."""
    return record_observation(robot.directory, cycle,
                              partial(collect, session=source.directory), configuration=config)


def test_empty_session_and_disabled_modalities(sources):
    """Return immediately for empty or disabled input without starting inference."""
    robot, source, config = sources
    value, files = persist(robot, source, config)
    assert json.loads(value)['read']['observations'] == [] and files == []
    assert modalities(config) == dict(hear=False, vision=False, read=True)
    assert modalities({}) == dict(hear=False, vision=True, read=False)
    assert modalities({}, 'all') == dict(hear=True, vision=True, read=False)


def test_incremental_roles_order_provenance_and_read_only(sources):
    """Consume User entries once without modifying any source artifact.

    Check every source byte before asking the conversation owner to append a
    fourth message. That legitimate append may extend its observation index,
    but must preserve every earlier index byte and historical stage record.
    """
    robot, source, config = sources
    for message in ('first', 'same', 'same'):
        exchange(source, message, reply, {})
    before = {str(p.relative_to(source.directory)): p.read_bytes()
              for p in source.directory.rglob('*') if p.is_file()}
    pending = observe(robot.directory, source.directory)
    assert [x['content'] for x in pending['observations']] == ['first', 'same', 'same']
    assert [x['message_index'] for x in pending['observations']] == [1, 3, 5]
    assert all(x['role'] == 'user' and x['epistemic_status'] == 'unverified_user_statement' and x['timestamp'] and x['source_record']
               for x in pending['observations'])
    assert len(observe(robot.directory, source.directory)['observations']) == 3
    persist(robot, source, config)
    assert observe(robot.directory, source.directory)['observations'] == []
    assert {str(p.relative_to(source.directory)): p.read_bytes()
            for p in source.directory.rglob('*') if p.is_file()} == before
    exchange(source, 'fourth', reply, {})
    assert [x['content'] for x in observe(robot.directory, source.directory)['observations']] == ['fourth']
    for path, content in before.items():
        if path == 'observations/index.jsonl':
            assert (source.directory / path).read_bytes().startswith(content)
        elif path not in ('conversation.md', 'metadata.yaml', 'state/.invocations.json'):
            assert (source.directory / path).read_bytes() == content


def test_source_unchanged_by_read(sources):
    """Reading and committing a robot observation never write the conversation."""
    robot, source, config = sources
    exchange(source, 'Keep my history.', reply, {})
    before = {p: p.read_bytes() for p in source.directory.rglob('*') if p.is_file()}
    persist(robot, source, config)
    assert {p: p.read_bytes() for p in source.directory.rglob('*') if p.is_file()} == before


def test_restart_recovers_cursor_from_committed_sense(sources):
    """A new interpreter recovers consumption without global or conversation state."""
    robot, source, config = sources
    exchange(source, 'once', reply, {})
    persist(robot, source, config)
    command = [sys.executable, '-c',
               'import json,sys; from sense.read import observe; print(json.dumps(observe(sys.argv[1],sys.argv[2])))',
               str(robot.directory), str(source.directory)]
    restored = json.loads(subprocess.check_output(command, text=True))
    assert restored['observations'] == [] and restored['cursor']['message_index'] == 1
    other = MemoryManager(robot.directory.parent.parent, 'other', goal='Independent reader.')
    assert len(observe(other.directory, source.directory)['observations']) == 1


def test_failed_sense_does_not_consume(sources):
    """A callback failure records failure and leaves the proposed batch retryable."""
    robot, source, config = sources
    exchange(source, 'retry', reply, {})

    def fail(directory, cycle):
        """Read the pending message, then emulate another modality failing."""
        assert observe(directory, source.directory)['observations']
        raise RuntimeError('other modality failed')

    with pytest.raises(RuntimeError, match='other modality'):
        record_observation(robot.directory, 1, fail, configuration=config)
    failed = json.loads((robot.directory / 'state/cycle_000001/sense.json').read_text())
    assert failed['status'] == 'failed' and 'read_cursor' not in failed
    assert len(observe(robot.directory, source.directory)['observations']) == 1
    persist(robot, source, config, 2)
    assert observe(robot.directory, source.directory)['observations'] == []


def test_failed_persistence_does_not_consume(sources, monkeypatch):
    """A failed atomic stage commit leaves no cursor and permits exact replay."""
    import os
    robot, source, config = sources
    exchange(source, 'disk retry', reply, {})
    original = os.link

    def fail(source_path, destination):
        """Reject only the final stage publication to emulate a storage failure."""
        raise OSError('disk failure')

    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(OSError, match='disk failure'):
        persist(robot, source, config)
    assert not list((robot.directory / 'state/cycle_000001').iterdir())
    assert len(observe(robot.directory, source.directory)['observations']) == 1
    monkeypatch.setattr(os, 'link', original)
    persist(robot, source, config)
    assert observe(robot.directory, source.directory)['observations'] == []


def test_failed_later_cycle_does_not_replay_successful_sense(sources):
    """Keep an already committed Read cursor after a later Model failure.

    Persist the first observation, fail the existing world-model generator, then
    make a new Sense record and confirm the committed message is not replayed.
    """
    from smpa_ros.model import generate_world_model
    robot, source, config = sources
    exchange(source, 'already observed', reply, {})
    value, _ = persist(robot, source, config)
    model = MagicMock(generate=MagicMock(side_effect=RuntimeError('model failed')))
    with pytest.raises(RuntimeError, match='model failed'):
        generate_world_model(robot.directory, 1, value, model, observations=[], pipeline=config)
    assert observe(robot.directory, source.directory)['observations'] == []
    value, _ = persist(robot, source, config, 2)
    assert json.loads(value)['read']['observations'] == []


def test_session_isolation_and_resumption(sources, tmp_path):
    """Conversation resume retains message identity and other sessions do not mix."""
    robot, source, config = sources
    exchange(source, 'original', reply, {})
    persist(robot, source, config)
    source.finalize('completed')
    restored = open_conversation(tmp_path, resume=source.directory)
    exchange(restored, 'after resume', reply, {})
    assert [x['content'] for x in observe(robot.directory, restored.directory)['observations']] == ['after resume']
    other = open_conversation(tmp_path / 'other')
    exchange(other, 'other session', reply, {})
    assert [x['content'] for x in observe(robot.directory, other.directory)['observations']] == ['other session']


def test_incomplete_writes_deferred_without_locking(sources):
    """A half-written transcript or stage returns no batch and never advances."""
    robot, source, config = sources
    append_message(source.directory, 'User', 'not yet staged')
    pending = observe(robot.directory, source.directory)
    assert pending['observations'] == [] and 'cursor' not in pending and 'deferred' in pending
    with (source.directory / 'conversation.md').open('ab') as output:
        output.write(b'<!-- smpa-message User 200 -->\n')
    pending = observe(robot.directory, source.directory)
    assert pending['observations'] == [] and 'cursor' not in pending


def test_changed_consumed_history_is_rejected(sources):
    """A valid but rewritten transcript cannot redirect a recovered cursor."""
    robot, source, config = sources
    exchange(source, 'first', reply, {})
    persist(robot, source, config)
    path = source.directory / 'conversation.md'
    path.write_bytes(path.read_bytes().replace(b'first', b'other'))
    with pytest.raises(ValueError, match='history changed'):
        observe(robot.directory, source.directory)


@pytest.mark.parametrize('config', [
    {'sensing': {'read': True}}, {'sensing': {'hear': 'yes'}},
    {'sensing': {'read': {'enabled': True}}}, {'sensing': {'unknown': True}},
])
def test_invalid_modality_configuration(config):
    """Reject malformed switches before any model or participant is started."""
    with pytest.raises(ValueError):
        modalities(config)


@pytest.mark.parametrize('enabled', [
    dict(hear=False, vision=False, read=True),
    dict(hear=True, vision=False, read=False),
    dict(hear=False, vision=True, read=False),
    dict(hear=True, vision=True, read=True),
])
def test_independent_and_combined_collection(sources, monkeypatch, enabled):
    """Select modalities independently and collect enabled ones in direct order."""
    import smpa_ros.sense as sensing
    robot, source, config = sources
    exchange(source, 'attributed user claim', reply, {})
    config['sensing'].update(hear=enabled['hear'], vision=enabled['vision'])
    config['sensing']['read']['enabled'] = enabled['read']
    called = []
    path = robot.directory / 'observations/vision/description.md'
    path.write_text('visible scene')

    def vision(*args):
        """Mark visual acquisition without loading inference or adding publishers."""
        called.append('vision')
        return path

    def hear(client):
        """Mark auditory acquisition without accessing an audio device."""
        called.append('hear')
        return 'spoken text'

    monkeypatch.setattr(sensing, 'capture_once', vision)
    monkeypatch.setattr('sense.hear.observe', hear)
    value, transcript = sensing.observe(None, config, robot.directory, None, 5, 'all', object())
    result = json.loads(value)
    assert ('vision' in result) == enabled['vision']
    assert ('speech' in result) == enabled['hear']
    assert ('read' in result) == enabled['read']
    assert called == [name for name in ('vision', 'hear') if enabled[name]]
    assert transcript == ('spoken text' if enabled['hear'] else '')


def test_independent_stage_nodes_do_not_load_backends(sources, tmp_path):
    """Start four independent stage nodes without constructing model weights.

    Every node owns its own action server and uses the same configured memory
    root. Model loading remains the inference server's responsibility; stage
    construction itself must not create a backend or invoke another stage.
    """
    import rclpy
    from rclpy.parameter import Parameter
    from smpa_ros.sense_node import SenseNode
    from smpa_ros.model_node import ModelNode
    from smpa_ros.plan_node import PlanNode
    from smpa_ros.act_node import ActNode
    robot, source, config = sources
    root = str(robot.directory.parent.parent)
    rclpy.init(args=[])
    nodes = []
    try:
        for kind in (SenseNode, ModelNode, PlanNode, ActNode):
            nodes.append(kind(parameter_overrides=[Parameter('memory_root', value=root)]))
        assert [node.get_name() for node in nodes] == ['sense_stage', 'model_stage', 'plan_stage', 'act_stage']
        assert all(node.get_parameter('memory_root').value == root for node in nodes)
        assert all(not hasattr(node, 'backend') for node in nodes)
        assert len({id(node.server) for node in nodes}) == 4
    finally:
        for node in reversed(nodes):
            node.destroy_node()
        rclpy.shutdown()


def test_corrupt_committed_robot_record_fails_closed(sources):
    """Unreadable committed history must not silently replay consumed messages."""
    robot, source, config = sources
    exchange(source, 'once', reply, {})
    persist(robot, source, config)
    (robot.directory / 'state/cycle_000001/sense.json').write_text('{')
    with pytest.raises(ValueError, match='Cannot recover Read cursor'):
        observe(robot.directory, source.directory)


def test_cached_refinement_does_not_replay_read_batch(sources):
    """Reuse frozen visual evidence while reading only newly persisted text.

    Commit the original visual and Read observation, add another user message,
    and invoke the actual independent Sense callback with refinement enabled.
    Confirm the cached image description survives and only the new text appears.
    """
    from types import SimpleNamespace
    from unittest.mock import Mock
    from smpa_ros.sense_node import SenseNode
    robot, source, config = sources
    pipeline = dict(config, enabled_actions=['observe'],
                    scene_refinement={'enabled': True, 'properties': ['color']},
                    stage_prompts={'sense': {'name': 'chosen', 'text': 'Describe.'}})
    memory = MemoryManager(robot.directory.parent.parent, 'refinement', goal='Read.',
                           configuration={'pipeline': pipeline})
    memory.begin_cycle()
    exchange(source, 'first batch', reply, {})
    name, provenance = memory.invocation('sense', 'chosen')
    def initial(directory, cycle):
        """Read the first batch alongside a fixed initial vision observation."""
        return json.dumps({'vision': 'frozen scene', 'read': observe(directory, source.directory)})
    record_observation(memory.directory, 1, initial, record_name=name, provenance=provenance,
                       prompt_name='chosen', configuration=pipeline)
    memory.write_refinement({'status': 'running', 'questions': []})
    exchange(source, 'new batch', reply, {})
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(
        value=1.0 if name == 'timeout' else str(robot.directory.parent.parent)),
        wait=Mock(), describe=Mock(), get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id']), succeed=Mock(), abort=Mock())
    result = SenseNode.execute(node, handle)
    handle.succeed.assert_called_once()
    output = json.loads(result.observation)
    assert output['vision'] == 'frozen scene'
    assert [item['content'] for item in output['read']['observations']] == ['new batch']
