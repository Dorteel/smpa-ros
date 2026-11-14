"""Read-only, incremental observations of one explicitly selected conversation.

A cursor is a transcript message index plus a prefix digest and session identity.
The authoritative cursor lives in successful robot Sense records, not in the
conversation and not in a second database. Merely reading never consumes input.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import yaml
from memory import MemoryManager
from smpa_ros.conversation import read_messages


def prefix_digest(messages):
    """Identify the exact ordered roles and text preceding a consumption boundary.

    Hash canonical UTF-8 JSON rather than mutable file metadata. A later append
    leaves the digest unchanged; replacing or truncating consumed history cannot
    silently make a saved robot-run cursor refer to different user statements.
    """
    return hashlib.sha256(json.dumps(
        [{'role': item['role'], 'content': item['content']} for item in messages],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def observe(robot_directory, session):
    """Snapshot newly persisted User messages without waiting or changing the source.

    Validate the assigned robot run and explicitly selected conversation identity.
    Recover this run's latest consumption boundary from successful Sense records;
    failed or incomplete records never consume messages. Read the existing
    length-delimited transcript and match User messages to their persisted Sense
    records, preserving ordering, source timestamps and provenance. A message
    whose stage record is still being written is deferred, not skipped.

    Reject changed consumed history. A transient incomplete transcript returns
    an empty attributed observation and no cursor, allowing retry without locks
    or polling. Return a proposed cursor with complete messages; only the robot
    Sense record writer commits it. This operation never starts conversational
    inference, modifies RDF, resumes a robot experiment or writes conversation
    files. A later robot stage failure does not undo already persisted sensing.
    """
    robot, _ = MemoryManager.assigned_run(str(Path(robot_directory).resolve()))
    source, identity = MemoryManager.assigned_run(str(Path(session).expanduser().resolve()))
    metadata = yaml.safe_load((source / 'metadata.yaml').read_text())
    if metadata.get('configuration', {}).get('kind') != 'conversation':
        raise ValueError('Read source must be a conversation session')
    if source == robot:
        raise ValueError('Read cannot consume its own robot run')
    previous = None
    for path in (robot / 'state').glob('cycle_*/sense*.json'):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise ValueError(f'Cannot recover Read cursor from {path}') from error
        cursor = record.get('read_cursor')
        if record.get('status') != 'generated' or not isinstance(cursor, dict):
            continue
        if cursor.get('session_path') != str(source):
            continue
        if cursor.get('session_id') != identity:
            raise ValueError('Read session identity changed at the selected path')
        if previous is None or cursor['message_index'] > previous['message_index']:
            previous = cursor
    result = dict(modality='read', epistemic_status='attributed_user_statements_not_verified_physical_facts', session_id=identity, session_path=str(source),
                  observed_at=datetime.now(timezone.utc).isoformat(), observations=[])
    try:
        messages = read_messages(source)
    except ValueError as error:
        # GUI appends are not an atomic file replacement; never wait on its lock.
        if 'Incomplete' not in str(error):
            raise
        result['deferred'] = str(error)
        return result
    boundary = previous['message_index'] if previous else 0
    if boundary > len(messages) or (previous and prefix_digest(messages[:boundary]) != previous['prefix_sha256']):
        raise ValueError('Previously consumed conversation history changed')
    stages = []
    for path in (source / 'state').glob('cycle_*/sense*.json'):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get('stage') == 'sense' and record.get('status') == 'generated':
            stages.append((record.get('sequence', 0), path, record))
    stages.sort(key=lambda item: (item[0], str(item[1])))
    expected = sum(message['role'] == 'user' for message in messages)
    if len(stages) < expected:
        result['deferred'] = 'User message provenance is not yet complete'
        return result
    if len(stages) != expected:
        raise ValueError('Conversation transcript and Sense record counts disagree')
    user_number = 0
    committed = boundary
    for index, message in enumerate(messages, 1):
        if message['role'] != 'user':
            continue
        user_number += 1
        if index <= boundary:
            continue
        if user_number > len(stages):
            break
        _, path, record = stages[user_number - 1]
        if record.get('output') != message['content']:
            # A half-written stage must not cause later identical text to shift.
            raise ValueError('Conversation message and Sense provenance disagree')
        result['observations'].append(dict(
            modality='read', role='user', epistemic_status='unverified_user_statement', content=message['content'],
            message_index=index, timestamp=record.get('started_at'),
            source_record=str(path), source=str(source / 'conversation.md') + f'#message-{index}',
            provenance={key: value for key, value in record.items() if key != 'output'}))
        committed = index
    result['cursor'] = dict(session_id=identity, session_path=str(source),
                            message_index=committed, prefix_sha256=prefix_digest(messages[:committed]))
    return result
