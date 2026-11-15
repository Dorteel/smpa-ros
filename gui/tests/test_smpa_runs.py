"""Tests of the read-only SMPA session adapter against fixture run directories in SMPA's formats."""
import hashlib
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
import smpa_memory as fixtures  # noqa: E402
from backend import smpa_runs  # noqa: E402


@pytest.fixture()
def memory(tmp_path):
    """Build a fresh fixture memory directory for one test."""
    return fixtures.build(tmp_path / 'memory')


def tree_fingerprint(root):
    """Fingerprint every file's content, size and modification time under a directory."""
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob('*')):
        if path.is_file():
            info = path.stat()
            digest.update(f'{path.relative_to(root)}|{info.st_size}|{info.st_mtime_ns}|'.encode() + path.read_bytes())
    return digest.hexdigest()


def names_with(*names):
    """Build a ROS name collection that contains the given names as nodes and topics."""
    return dict(all=set(names), nodes=set(names), topics=set(names))


def by_id(sessions):
    """Index sessions by id."""
    return {item['session_id']: item for item in sessions}


def test_sessions_are_classified(memory):
    """Kinds, lifecycle, activity, modalities and the Read binding come from metadata and ROS names."""
    active_node = '/run_' + fixtures.ACTIVE.replace('-', '_') + '/sense/vision/smpa_loop'
    sessions = by_id(smpa_runs.list_sessions(memory, names_with(active_node)))
    assert set(sessions) == {fixtures.ROBOT, fixtures.CONVERSATION, fixtures.LEGACY_LAYOUT, fixtures.REFINEMENT,
                             fixtures.ACTIVE, fixtures.STALE, fixtures.EMPTY, fixtures.BROKEN, smpa_runs.LEGACY_ID}
    robot = sessions[fixtures.ROBOT]
    assert (robot['kind'], robot['status'], robot['activity']) == ('robot', 'completed', 'ended')
    assert robot['modalities'] == dict(vision=True, hear=True, read=True) and robot['camera_topic'] == fixtures.CAMERA_TOPIC
    assert robot['read_session']['session_id'] == fixtures.CONVERSATION
    assert sessions[fixtures.ACTIVE]['activity'] == 'active' and sessions[fixtures.ACTIVE]['ros']['nodes'] == [active_node]
    assert sessions[fixtures.STALE]['activity'] == 'running_not_visible'
    assert sessions[fixtures.CONVERSATION]['activity'] == 'running_unverified'
    assert sessions[fixtures.CONVERSATION]['modalities'] == dict(vision=False, hear=False, read=True)
    assert sessions[fixtures.LEGACY_LAYOUT]['modalities'] == dict(vision=True, hear=False, read=False)
    assert sessions[fixtures.BROKEN]['kind'] == 'unknown' and sessions[fixtures.BROKEN]['activity'] == 'unknown'
    assert sessions[fixtures.BROKEN]['metadata_error'].startswith('unreadable')
    assert sessions[smpa_runs.LEGACY_ID]['kind'] == 'legacy_archive'
    started = [item['started_at'] for item in smpa_runs.list_sessions(memory) if item['started_at']]
    assert started == sorted(started, key=smpa_runs.parse_time, reverse=True)
    without_ros = by_id(smpa_runs.list_sessions(memory, None))
    assert without_ros[fixtures.ACTIVE]['activity'] == 'running_unverified', 'no ROS graph means liveness is unknown'


def test_robot_run_observations_and_provenance(memory):
    """All modalities of a robot run appear in time order with time sources and links to Sense records."""
    result = smpa_runs.session_observations(memory, fixtures.ROBOT)
    entries = result['observations']
    assert [entry['t'] for entry in entries] == sorted(entry['t'] for entry in entries)
    kinds = [(entry['modality'], entry['kind']) for entry in entries]
    assert kinds == [('read', 'user_message'), ('read', 'user_message'), ('sense', 'sense_record'),
                     ('vision', 'scene_description'), ('hear', 'audio_transcript'), ('sense', 'sense_record'),
                     ('vision', 'camera_frame')]
    description = entries[3]
    assert description['timestamp'] == '2026-11-15T09:00:05.250000+00:00' and description['timestamp_source'] == 'file name (UTC)'
    assert description['sense_record'] == 'state/cycle_000001/sense.json' and description['image'].endswith('.png')
    assert description['provenance']['camera']['frame_id'] == 'fixture_camera' and description['recorded'] is True
    audio = entries[4]
    assert audio['text'] == 'Please look at the red card.' and audio['provenance']['duration_s'] == 0.5
    assert audio['audio'].endswith('recording.wav') and audio['sense_record'] == 'state/cycle_000001/sense.json'
    first_read = entries[0]
    assert first_read['epistemic_status'] == 'unverified_user_statement' and first_read['provenance']['message_index'] == 1
    assert first_read['provenance']['conversation_session_id'] == fixtures.CONVERSATION
    failed = entries[5]
    assert failed['status'] == 'failed' and 'TimeoutError' in failed['text']
    frame = entries[6]
    assert frame['status'] == 'evidence_without_description' and frame['sense_record'] == 'state/cycle_000002/sense.json'
    assert frame['timestamp_source'] == 'PNG smpa.camera received_at'
    assert len({entry['id'] for entry in entries}) == len(entries)
    assert [entry['kind'] for entry in smpa_runs.session_observations(memory, fixtures.ROBOT, 'hear')['observations']] == ['audio_transcript']


def test_other_session_layouts(memory):
    """Conversation, legacy layout, refinement, archive, empty and damaged sessions are read without errors."""
    conversation = smpa_runs.session_observations(memory, fixtures.CONVERSATION)['observations']
    assert [entry['text'] for entry in conversation] == ['There is a red card on the table.', 'The card belongs to Ana.']
    assert all(entry['modality'] == 'read' and entry['provenance']['role'] == 'user' for entry in conversation)
    legacy = smpa_runs.session_observations(memory, fixtures.LEGACY_LAYOUT)['observations']
    description = next(entry for entry in legacy if entry['kind'] == 'scene_description')
    assert description['timestamp'] == '2026-10-23T16:27:41.221845+02:00'
    assert "run's start offset" in description['timestamp_source'] and description['source'].startswith('vision/')
    refinement = smpa_runs.session_observations(memory, fixtures.REFINEMENT)['observations']
    focused = next(entry for entry in refinement if entry['kind'] == 'focused_observation')
    assert focused['text'] == 'Green.' and focused['status'] == 'answered' and focused['provenance']['target'] == 'entity_1'
    assert focused['sense_record'] == 'state/cycle_000002/sense.json', 'linked through focused_observations[].source_record'
    assert any(entry['status'] == 'frozen_for_refinement' for entry in refinement)
    archive = smpa_runs.session_observations(memory, smpa_runs.LEGACY_ID)['observations']
    assert {entry['kind'] for entry in archive} == {'scene_description', 'audio_transcript'}
    assert smpa_runs.session_observations(memory, fixtures.EMPTY)['observations'] == []
    assert smpa_runs.session_observations(memory, fixtures.BROKEN)['observations'] == []
    assert smpa_runs.session_observations(memory, 'no-such-session') is None


def test_damaged_records_are_reported_not_fatal(memory):
    """An unreadable Sense record or focused answer becomes an entry that says so."""
    directory = smpa_runs.find_session(memory, fixtures.ROBOT)
    (directory / 'state/cycle_000003').mkdir()
    (directory / 'state/cycle_000003/sense.json').write_text('{"stage": "sense", ')
    (directory / 'observations/vision/observe_000003_001.json').write_text('not json')
    entries = smpa_runs.session_observations(memory, fixtures.ROBOT)['observations']
    assert any(entry['kind'] == 'sense_record' and entry['status'].startswith('unreadable') for entry in entries)
    assert any(entry['kind'] == 'focused_observation' and entry['status'].startswith('unreadable') for entry in entries)


def test_revision_follows_changes(memory):
    """The revision stays equal while nothing changes and differs after SMPA writes a file."""
    first = smpa_runs.session_observations(memory, fixtures.ACTIVE)['revision']
    assert smpa_runs.session_observations(memory, fixtures.ACTIVE)['revision'] == first
    directory = smpa_runs.find_session(memory, fixtures.ACTIVE)
    (directory / 'observations/vision/2026-11-15_10-00-00_000000Z_sense_output.md').write_text('New.')
    later = smpa_runs.session_observations(memory, fixtures.ACTIVE)
    assert later['revision'] != first and later['observations'][0]['text'] == 'New.'


def test_reading_changes_nothing(memory):
    """Listing sessions, reading observations and resolving files leave every file byte- and time-identical."""
    before = tree_fingerprint(memory)
    for session in smpa_runs.list_sessions(memory, names_with()):
        result = smpa_runs.session_observations(memory, session['session_id'])
        for entry in result['observations']:
            if entry['source']:
                smpa_runs.recorded_file(memory, session['session_id'], entry['source'])
    assert tree_fingerprint(memory) == before


def test_file_access_is_confined(memory, tmp_path):
    """Only existing recorded media files inside the session directory can be resolved."""
    robot = fixtures.ROBOT
    found = smpa_runs.recorded_file(memory, robot, 'observations/vision/2026-11-15_09-00-05_250000Z_sense_output.png')
    assert found[1] == 'image/png'
    assert smpa_runs.recorded_file(memory, robot, 'metadata.yaml') is None, 'YAML is not a recorded media type'
    assert smpa_runs.recorded_file(memory, robot, '../') is None
    assert smpa_runs.recorded_file(memory, robot, '/etc/passwd') is None
    assert smpa_runs.recorded_file(memory, robot, '../' + smpa_runs.find_session(memory, fixtures.CONVERSATION).name
                                   + '/state/cycle_000001/sense.json') is None
    secret = tmp_path / 'secret.md'
    secret.write_text('outside')
    os.symlink(secret, smpa_runs.find_session(memory, robot) / 'link.md')
    assert smpa_runs.recorded_file(memory, robot, 'link.md') is None, 'links leading outside are refused'
    assert smpa_runs.recorded_file(memory, 'unknown', 'goal.md') is None


def test_modality_rules_follow_smpa():
    """Legacy service defaults, explicit switches, opt-in Read and malformed settings."""
    assert smpa_runs.modalities(dict(service='speech')) == dict(vision=False, hear=True, read=False)
    assert smpa_runs.modalities(dict(service='all', pipeline=dict(sensing=dict(vision=False)))) == dict(vision=False, hear=True, read=False)
    assert smpa_runs.modalities(dict(pipeline=dict(sensing=dict(read=dict(enabled=True, session='/x'))))) == dict(vision=True, hear=False, read=True)
    assert smpa_runs.modalities(dict(pipeline=dict(sensing=dict(hear='yes'))))['hear'] is None
    assert smpa_runs.modalities(dict(pipeline=dict(sensing=[1]))) == dict(vision=None, hear=None, read=None)


def test_default_root(tmp_path):
    """The default memory root is memory/ beside gui/ only when it contains runs/."""
    gui = tmp_path / 'smpa-ros' / 'gui'
    gui.mkdir(parents=True)
    assert smpa_runs.default_root(gui) is None
    (tmp_path / 'smpa-ros' / 'memory' / 'runs').mkdir(parents=True)
    assert smpa_runs.default_root(gui) == (tmp_path / 'smpa-ros' / 'memory').resolve()
