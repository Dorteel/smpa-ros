"""Verify session liveness and evidence contracts without models or hardware."""
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
from threading import Event
from time import monotonic

import numpy as np
from PIL import Image
import pytest
import yaml

from memory import MemoryManager, append_observation
from smpa_ros.conversation import exchange, open_conversation
from smpa_ros.conversation_web import make_server
from sense.vision import freeze_image
from speech.observation import AudioObservation


def read_index(directory):
    """Read every complete manifest written by this test's evidence producers.

    Decode JSON lines in their durable order. Missing or malformed entries fail
    the test rather than being hidden by a tolerant consumer.
    """
    return [json.loads(line) for line in (directory / 'observations/index.jsonl').read_text().splitlines()]


def reply(history, **settings):
    """Acknowledge a persisted user message without loading a reply model.

    The persistence tests need an ordinary successful response only. Ignore
    generation controls and preserve their real transcript and stage writers.
    """
    return 'Acknowledged'


def append_from_process(directory, number):
    """Append three independent observations from a spawned writer process.

    Use no ROS context or model. Give each writer distinct text so the parent
    can prove none of the simultaneously written lines was lost or interleaved.
    """
    for index in range(3):
        append_observation(Path(directory), modality='read', kind='user_message',
            observed_at=datetime.now(timezone.utc).isoformat(), status='generated',
            text=f'{number}:{index}', epistemic_status='unverified_user_statement')


def own_session(root, connection):
    """Publish one real child process's heartbeat until its parent kills it.

    Send the assigned run directory only after the first heartbeat is durable.
    Wait indefinitely without inference so killing this process tests stale
    metadata rather than a cooperative finalization path.
    """
    memory = MemoryManager(root, goal='Heartbeat fixture')
    memory.start_heartbeat()
    connection.send(str(memory.directory))
    connection.close()
    Event().wait()


def test_heartbeat_merges_stage_updates_and_stops_on_finalize(tmp_path):
    """Keep a quiet owner live while another manager updates the run.

    Wait for one actual five-second refresh, then let the original manager
    complete its cycle. The heartbeat must retain the stage edit, and the
    owner's stale in-memory snapshot must retain the refreshed process block.
    Finalization must join the heartbeat and record a terminal state.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    memory.begin_cycle()
    memory.start_heartbeat()
    try:
        first = memory.metadata['process'].copy()
        assert first['pid'] == os.getpid()
        assert first['hostname'] and first['heartbeat_interval_s'] == 5
        assert datetime.fromisoformat(first['started_at']) <= datetime.fromisoformat(first['heartbeat_at'])
        stage = MemoryManager.attach_active_run(tmp_path, memory.metadata['run_id'])
        assert not hasattr(stage, '_heartbeat_thread')
        stage.metadata['stage_note'] = 'preserved'
        stage.write_metadata()
        deadline = monotonic() + 8
        changed = Event()
        while monotonic() < deadline:
            latest = yaml.safe_load((memory.directory / 'metadata.yaml').read_text())
            if latest['process']['heartbeat_at'] != first['heartbeat_at']:
                break
            changed.wait(.05)
        else:
            pytest.fail('The idle owner did not refresh its heartbeat')
        memory.complete_cycle()
        assert memory.metadata['stage_note'] == 'preserved'
        assert memory.metadata['process'] == latest['process']
    finally:
        memory.finalize('completed')
    assert not memory._heartbeat_thread.is_alive()
    assert yaml.safe_load((memory.directory / 'metadata.yaml').read_text())['status'] == 'completed'


def test_heartbeat_retries_a_transient_write_failure(tmp_path, monkeypatch, capfd):
    """Recover a session refresh after a visible transient storage error.

    Fail only the heartbeat thread's first write, leaving the owner and evidence
    writers untouched. Require a logged error followed by a newer durable
    heartbeat on the next interval, then stop the worker through finalization.
    """
    from threading import current_thread
    memory = MemoryManager(tmp_path, goal='Observe')
    original = MemoryManager.write_metadata
    failed = Event()
    def publish(manager):
        """Fail one background refresh and retain ordinary metadata behavior."""
        if current_thread().name.startswith('smpa-heartbeat-') and not failed.is_set():
            failed.set()
            raise OSError('fixture transient storage error')
        return original(manager)
    monkeypatch.setattr(MemoryManager, 'write_metadata', publish)
    memory.start_heartbeat()
    initial = memory.metadata['process']['heartbeat_at']
    try:
        assert failed.wait(8)
        deadline = monotonic() + 8
        while monotonic() < deadline:
            latest = yaml.safe_load((memory.directory / 'metadata.yaml').read_text())
            if latest['process']['heartbeat_at'] != initial:
                break
            Event().wait(.05)
        else:
            pytest.fail('Heartbeat did not recover after a failed refresh')
        assert 'Session heartbeat could not be written' in capfd.readouterr().err
    finally:
        memory.finalize('completed')


def test_killed_owner_leaves_a_stale_heartbeat(tmp_path):
    """Retain factual running metadata when a process dies without cleanup.

    Kill a spawned owner after its first publication. Its heartbeat must no
    longer advance, its PID must identify the child, and the documented GUI
    expiry rule must classify that retained timestamp as inactive after fifteen
    seconds. Do not fabricate a terminal outcome for an unobserved crash.
    """
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=own_session, args=(str(tmp_path), send))
    process.start()
    send.close()
    try:
        assert receive.poll(10), 'Owner did not publish startup'
        directory = Path(receive.recv())
        process.kill()
        process.join(timeout=5)
        assert not process.is_alive()
        metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
        assert metadata['status'] == 'running' and metadata['process']['pid'] == process.pid
        before = (directory / 'metadata.yaml').read_bytes()
        Event().wait(.1)
        assert (directory / 'metadata.yaml').read_bytes() == before
        heartbeat = datetime.fromisoformat(metadata['process']['heartbeat_at'])
        later = heartbeat + timedelta(seconds=16)
        assert (later - heartbeat).total_seconds() >= 3 * metadata['process']['heartbeat_interval_s']
    finally:
        receive.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def test_conversation_server_owns_heartbeat_until_close(tmp_path):
    """Start liveness for a real HTTP owner without loading reply inference.

    Closing the server must stop its worker and release the transcript lease.
    Preserve existing lifecycle ownership: server close alone does not invent
    whether the conversation completed or was interrupted.
    """
    memory = open_conversation(tmp_path)
    server = make_server(memory, reply, {}, port=0)
    try:
        assert yaml.safe_load((memory.directory / 'metadata.yaml').read_text())['process']['pid'] == os.getpid()
    finally:
        server.server_close()
        memory.finalize('completed')
    assert not memory._heartbeat_thread.is_alive()


def test_index_appends_are_serialized_between_processes(tmp_path):
    """Append concurrent evidence without replacing an earlier manifest prefix.

    Keep an initial entry, spawn two independent writers, and require every
    subsequent line to remain valid and uniquely identified for this run.
    """
    memory = MemoryManager(tmp_path, goal='Observe')
    append_from_process(str(memory.directory), 'initial')
    prefix = (memory.directory / 'observations/index.jsonl').read_bytes()
    context = multiprocessing.get_context('spawn')
    processes = [context.Process(target=append_from_process, args=(str(memory.directory), number)) for number in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert (memory.directory / 'observations/index.jsonl').read_bytes().startswith(prefix)
        entries = read_index(memory.directory)
        assert len(entries) == len({entry['observation_id'] for entry in entries}) == 9
        assert {entry['text'] for entry in entries} == {f'{n}:{i}' for n in ('initial', 0, 1) for i in range(3)}
        assert all(entry['schema'] == 'smpa.observation/1' and entry['session_id'] == memory.metadata['run_id'] for entry in entries)
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)


def test_index_write_is_one_append_and_recovers_from_failure(tmp_path, monkeypatch):
    """Keep an append immutable and release its lock after a failed write.

    Observe one actual OS write per entry. A failure before that write must
    preserve prior bytes and allow the next producer to append normally.
    """
    import memory as module
    memory = MemoryManager(tmp_path, goal='Observe')
    original = os.write
    calls = []
    def write(descriptor, line):
        """Capture the complete line before performing its one actual append."""
        calls.append(line)
        return original(descriptor, line)
    monkeypatch.setattr(module.os, 'write', write)
    append_from_process(str(memory.directory), 'initial')
    assert len(calls) == 3 and all(line.endswith(b'\n') for line in calls)
    before = (memory.directory / 'observations/index.jsonl').read_bytes()
    def fail(descriptor, line):
        """Simulate a storage error before any bytes are added to the index."""
        raise OSError('fixture disk failure')
    monkeypatch.setattr(module.os, 'write', fail)
    with pytest.raises(OSError, match='disk failure'):
        append_from_process(str(memory.directory), 'failed')
    assert (memory.directory / 'observations/index.jsonl').read_bytes() == before
    monkeypatch.setattr(module.os, 'write', write)
    append_from_process(str(memory.directory), 'recovered')
    assert len(read_index(memory.directory)) == 6


def test_partial_index_tail_is_preserved_for_explicit_recovery(tmp_path):
    """Refuse to concatenate a new entry onto a killed writer's partial line.

    Preserve the entire valid prefix and partial tail. This append-only contract
    cannot silently truncate history, so report the exact recovery requirement.
    """
    memory = MemoryManager(tmp_path, goal='Observe')
    append_from_process(str(memory.directory), 'initial')
    index = memory.directory / 'observations/index.jsonl'
    with index.open('ab') as output:
        output.write(b'{"schema":')
    before = index.read_bytes()
    with pytest.raises(RuntimeError, match='incomplete tail'):
        append_from_process(str(memory.directory), 'new')
    assert index.read_bytes() == before


def test_short_index_write_preserves_history_and_blocks_later_append(tmp_path, monkeypatch):
    """Expose a partial OS append without silently rewriting its damaged tail.

    Save a valid prefix, force the next write to publish only half its bytes,
    and require both the initial error and the later explicit recovery error.
    Neither error may change any byte from the earlier successful manifests.
    """
    memory = MemoryManager(tmp_path, goal='Observe')
    append_from_process(str(memory.directory), 'initial')
    index = memory.directory / 'observations/index.jsonl'
    prefix = index.read_bytes()
    original = os.write
    def partial(descriptor, line):
        """Publish half a manifest to exercise the real short-write boundary."""
        return original(descriptor, line[:len(line) // 2])
    monkeypatch.setattr(os, 'write', partial)
    with pytest.raises(OSError, match='append was incomplete'):
        append_from_process(str(memory.directory), 'partial')
    damaged = index.read_bytes()
    assert damaged.startswith(prefix) and len(damaged) > len(prefix)
    monkeypatch.setattr(os, 'write', original)
    with pytest.raises(RuntimeError, match='explicit recovery'):
        append_from_process(str(memory.directory), 'later')
    assert index.read_bytes() == damaged


def test_index_rejects_unknown_evidence_and_naive_times(tmp_path):
    """Reject fabricated evidence references and ambiguous local timestamps.

    Unknown files and paths outside the assigned run cannot become GUI evidence.
    An inline user message is valid without a file, but still needs an offset.
    """
    memory = MemoryManager(tmp_path, goal='Observe')
    values = dict(modality='read', kind='user_message', observed_at=datetime.now(timezone.utc).isoformat(), status='generated')
    for path in ('missing.md', '../outside.md', str(tmp_path / 'outside.md')):
        with pytest.raises(ValueError):
            append_observation(memory.directory, files={'text': path}, **values)
    values['observed_at'] = '2026-11-18T12:00:00'
    with pytest.raises(ValueError, match='UTC offset'):
        append_observation(memory.directory, **values)


def test_frozen_evidence_has_explicit_camera_times_and_is_immutable(tmp_path):
    """Freeze one image with receipt and freeze times without replacing history.

    A later attempt to freeze different pixels must leave both the first PNG
    and its one index line unchanged. The actual publisher stamp is retained.
    """
    memory = MemoryManager(tmp_path, goal='Observe')
    camera = dict(topic='/camera', received_at=datetime.now(timezone.utc).isoformat(), stamp={'sec': 12, 'nanosec': 34})
    freeze_image(memory.directory, Image.new('RGB', (2, 1), 'red'), camera)
    path = memory.directory / 'observations/vision/frozen_image.png'
    before = path.read_bytes()
    with Image.open(path) as stored:
        saved = json.loads(stored.info['smpa.camera'])
    assert saved['stamp'] == camera['stamp'] and saved['received_at'] == camera['received_at']
    assert datetime.fromisoformat(saved['frozen_at']).utcoffset() is not None
    freeze_image(memory.directory, Image.new('RGB', (2, 1), 'blue'), camera)
    assert path.read_bytes() == before
    assert len(read_index(memory.directory)) == 1
    assert read_index(memory.directory)[0]['provenance'] == saved


def test_restarted_vision_service_uses_its_persisted_frozen_frame(tmp_path):
    """Keep legacy service inference consistent with immutable refinement pixels.

    Freeze a red frame, then give a fresh service cache a blue request. Mock
    only inference and ROS node logging; the real description method must use
    the original red image and leave its camera provenance and index intact.
    """
    from types import SimpleNamespace
    from unittest.mock import Mock
    from sensor_msgs.msg import Image as RosImage
    from smpa_ros_interfaces.srv import DescribeScene
    from sense.vision import SenseService
    memory = MemoryManager(tmp_path, goal='Observe')
    camera = dict(topic='/camera', received_at=datetime.now(timezone.utc).isoformat())
    freeze_image(memory.directory, Image.new('RGB', (1, 1), 'red'), camera)
    frozen = memory.directory / 'observations/vision/frozen_image.png'
    before = frozen.read_bytes()
    backend = Mock()
    backend.describe.return_value = 'Original frame'
    node = SimpleNamespace(image=None, refinement={'enabled': True}, world_model_dir=memory.directory,
        pipeline={'camera_topic': '/camera'}, get_logger=Mock(return_value=Mock()), backend=backend,
        generation_options={}, default_prompt='Describe')
    request = DescribeScene.Request(image=RosImage(width=1, height=1, step=3, encoding='rgb8', data=[0, 0, 255]))
    response = SenseService.describe(node, request, DescribeScene.Response())
    assert response.description == 'Original frame' and not response.error
    assert backend.describe.call_args.args[0].getpixel((0, 0)) == (255, 0, 0)
    assert frozen.read_bytes() == before and len(read_index(memory.directory)) == 1


def test_audio_and_failed_conversation_keep_attributed_observations(tmp_path):
    """Index real persistence while mocking only reply and speech computation.

    Preserve exact audio-file paths and explicit capture time. A failed reply
    must still leave the unverified user message and its durable Sense identity
    discoverable, without publishing any assistant text as a user observation.
    """
    memory = open_conversation(tmp_path)
    audio = AudioObservation(memory.directory, np.zeros(16, dtype=np.float32), 'en', 'fixture/whisper')
    audio.write_transcript('complete', 'hello')
    def fail(history, **settings):
        """Fail reply inference only after the user's evidence has been saved."""
        raise RuntimeError('fixture inference unavailable')
    with pytest.raises(RuntimeError, match='inference unavailable'):
        exchange(memory, 'Unverified cup claim', fail, {})
    entries = read_index(memory.directory)
    assert [entry['modality'] for entry in entries] == ['hear', 'read']
    assert entries[0]['observed_at'] == audio.timestamp.isoformat()
    assert (memory.directory / entries[0]['files']['audio']).is_file()
    assert entries[0]['sense_record'] is None
    assert entries[1]['text'] == 'Unverified cup claim'
    assert entries[1]['epistemic_status'] == 'unverified_user_statement'
    assert entries[1]['sense_record'] == 'state/cycle_000001/sense.json'
    assert (memory.directory / entries[1]['sense_record']).is_file()
    memory.finalize('completed')


def test_read_index_preserves_source_and_commits_cursor_after_index(tmp_path, monkeypatch):
    """Attribute a robot's Read to the source without changing that conversation.

    Record the source's user message, then index a successful robot observation
    with the original message timestamp and source identity. A later index
    failure must persist a failed Sense without consuming its proposed cursor.
    """
    from smpa_ros.sense import record_observation
    from sense.read import observe
    import memory as module
    source = open_conversation(tmp_path / 'conversations')
    exchange(source, 'Cup claim', reply, {})
    robot = MemoryManager(tmp_path / 'robots', goal='Read claims')
    configuration = {'sensing': {'vision': False, 'hear': False,
                                  'read': {'enabled': True, 'session': str(source.directory)}}}
    before = {path: path.read_bytes() for path in source.directory.rglob('*') if path.is_file()}
    def read(directory, cycle):
        """Return the actual incremental Read result without doing inference."""
        return json.dumps({'read': observe(directory, source.directory)})
    robot.begin_cycle()
    record_observation(robot.directory, 1, read, configuration=configuration)
    manifest, = read_index(robot.directory)
    assert manifest['text'] == 'Cup claim' and manifest['cycle'] == 1
    assert manifest['provenance']['source_record'].startswith(str(source.directory))
    assert manifest['epistemic_status'] == 'unverified_user_statement'
    assert {path: path.read_bytes() for path in source.directory.rglob('*') if path.is_file()} == before
    robot.complete_cycle()
    robot.in_cycle = False
    exchange(source, 'Second claim', reply, {})
    robot.begin_cycle()
    def fail(*args, **kwargs):
        """Reject the manifest write before the proposed Read cursor is committed."""
        raise OSError('fixture index unavailable')
    monkeypatch.setattr(module, 'append_observation', fail)
    with pytest.raises(OSError, match='index unavailable'):
        record_observation(robot.directory, 2, read, configuration=configuration)
    failed = json.loads((robot.directory / 'state/cycle_000002/sense.json').read_text())
    assert failed['status'] == 'failed' and 'read_cursor' not in failed
    assert [item['content'] for item in observe(robot.directory, source.directory)['observations']] == ['Second claim']
    source.finalize('completed')
    robot.finalize('failed')
