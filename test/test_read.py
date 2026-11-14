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
from runner import Runner


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
    """Consume each persisted User entry once and leave the source byte-identical."""
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
    exchange(source, 'fourth', reply, {})
    assert [x['content'] for x in observe(robot.directory, source.directory)['observations']] == ['fourth']
    for path, content in before.items():
        if path not in ('conversation.md', 'metadata.yaml'):
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
    """Model failure after persisted sensing does not roll its cursor back."""
    robot, source, config = sources
    exchange(source, 'already observed', reply, {})
    with Runner(root=robot.directory.parent.parent,
                configuration={'pipeline': config},
                sense=partial(collect, session=source.directory),
                model=MagicMock(generate=MagicMock(side_effect=RuntimeError('model failed'))),
                plan=MagicMock(), act=MagicMock()) as runner:
        runner.start(goal='Test failed model cycle.')
        with pytest.raises(RuntimeError, match='model failed'):
            with runner.cycle():
                value = runner.sense()
                runner.model(value)
        assert observe(runner.memory.directory, source.directory)['observations'] == []
        with runner.cycle():
            assert json.loads(runner.sense())['read']['observations'] == []


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


def test_run_scoped_nodes_and_shared_backend(sources, monkeypatch):
    """Create native Runner nodes with unique modality namespaces and one backend."""
    import yaml
    import smpa_ros.backends.transformers as transformers
    robot, source, config = sources
    original = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config.yaml').read_text())
    original.update(config)
    path = robot.directory / 'test-config.yaml'
    path.write_text(yaml.safe_dump(original))
    backend = MagicMock()
    backend.model.device, backend.model.dtype = 'cpu', 'float32'
    constructor = MagicMock(return_value=backend)
    monkeypatch.setattr(transformers, 'TransformersVLM', constructor)
    monkeypatch.setattr('runner.resolve_vision_model', lambda *args, **kwargs:
                        dict(backend='transformers', path='/mock', model_id='test/shared'))
    with Runner(args=['test', '--config', str(path), '--vision', 'true',
                      '--world-model-root', str(robot.directory.parent.parent)]) as runner:
        runner.start(goal='Inspect sensor namespaces without actions.')
        prefix = '/run_' + runner.memory.metadata['run_id'].replace('-', '_') + '/sense'
        assert runner.camera_node.get_namespace() == prefix + '/vision'
        assert runner.sense_node.get_namespace() == prefix + '/vision'
        assert runner.read_node.get_namespace() == prefix + '/read'
        assert runner.sense_node.service.service_name == prefix + '/vision/describe_scene'
        assert runner._model is runner._plan is runner.backend is runner.sense_node.backend
        constructor.assert_called_once()


def test_corrupt_committed_robot_record_fails_closed(sources):
    """Unreadable committed history must not silently replay consumed messages."""
    robot, source, config = sources
    exchange(source, 'once', reply, {})
    persist(robot, source, config)
    (robot.directory / 'state/cycle_000001/sense.json').write_text('{')
    with pytest.raises(ValueError, match='Cannot recover Read cursor'):
        observe(robot.directory, source.directory)


def test_cached_refinement_does_not_replay_read_batch(sources):
    """Reuse frozen visual evidence while collecting only newly persisted text."""
    robot, source, config = sources
    exchange(source, 'first batch', reply, {})
    value, _ = persist(robot, source, config)
    exchange(source, 'new batch', reply, {})
    runner = Runner.__new__(Runner)
    runner.participant = None
    runner.refinement_config = {'enabled': True}
    runner.initial_observation = json.dumps({'vision': 'frozen scene', 'read': json.loads(value)['read']})
    runner.modalities = dict(hear=False, vision=True, read=True)
    runner.config = config
    result = json.loads(runner.observe(robot.directory, 2))
    assert result['vision'] == 'frozen scene'
    assert [item['content'] for item in result['read']['observations']] == ['new batch']
