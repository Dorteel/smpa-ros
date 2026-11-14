"""Preserve the laboratory as human-readable Markdown records.

Concepts: a conversation is an ordered sequence of immutable message events.
A feedback event records one human judgement without replacing earlier judgements.
An example is a correction (SFT) or explicit preference (DPO). A run snapshots
selected examples before any model is trained. Markdown frontmatter is authoritative;
the body is a readable rendering, never a second database.
"""
import copy
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml


def timestamp():
    """Return the current UTC time with its timezone for persistent records.

    Timestamps describe event time; unique identifiers, rather than clock ordering,
    distinguish events that happen at the same instant.
    """
    return datetime.now(timezone.utc).isoformat()


def identifier():
    """Create an identifier that can safely name one immutable record.

    A random UUID avoids collisions across browser sessions and application restarts.
    No existing identifier or record is changed.
    """
    return uuid.uuid4().hex


def write_record(root, kind, record):
    """Publish one new Markdown record without replacing any existing record.

    Root is the configured vault and kind is a fixed collection name. The record
    contains its stable identifier. First serialize frontmatter and a readable body,
    then flush a temporary file in the same directory. An exclusive hard link
    publishes the completed file atomically. A collision raises instead of overwriting
    history. The temporary file is removed even when publication fails.
    """
    if kind not in {'sessions', 'conversations', 'messages', 'feedback', 'training', 'metrics', 'events', 'models'}:
        raise ValueError('Unknown record collection')
    if not isinstance(record.get('id'), str) or not record['id'].isalnum():
        raise ValueError('Record identifier must be alphanumeric')
    directory = Path(root).resolve() / kind
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (record['id'] + '.md')
    body = f"# {record.get('title', kind.title())}\n\n"
    for key, value in record.items():
        if key in {'context', 'messages', 'dataset'}:
            body += f'## {key.title()}\n\n```json\n{json.dumps(value, indent=2, ensure_ascii=False)}\n```\n\n'
        else:
            body += f'**{key}**: {value}\n\n'
    content = '---\n' + yaml.safe_dump(record, allow_unicode=True, sort_keys=False) + '---\n\n' + body
    handle, temporary = tempfile.mkstemp(prefix='.pending-', dir=directory)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.unlink(temporary)
    return record


def records(root, kind):
    """Read a collection from its Markdown frontmatter in event order.

    Ignore unpublished temporary files. Invalid records raise a descriptive error
    rather than disappearing from experiments or silently changing dataset counts.
    Return fresh dictionaries, sorted by timestamp and stable identifier.
    """
    result = []
    for path in (Path(root) / kind).glob('*.md'):
        text = path.read_text(encoding='utf-8')
        if not text.startswith('---\n') or '\n---\n' not in text[4:]:
            raise ValueError(f'Invalid vault record: {path}')
        record = yaml.safe_load(text.split('\n---\n', 1)[0][4:])
        if not isinstance(record, dict) or record.get('id') != path.stem:
            raise ValueError(f'Invalid vault identifier: {path}')
        result.append(record)
    return sorted(result, key=lambda item: (item.get('created', ''), item['id']))


def append(root, kind, **fields):
    """Add timestamp and identity to a new event and publish it in the vault.

    Caller fields describe the event; callers cannot replace the generated identity
    or time. The returned dictionary is exactly the record saved to Markdown.
    """
    return write_record(root, kind, dict(fields, id=identifier(), created=timestamp()))


def conversation(root, title='New conversation'):
    """Start a conversation with a stable identity and no messages.

    The returned header can be selected again after restart. Existing conversations
    remain untouched when a user starts or restarts a conversation.
    """
    return append(root, 'conversations', title=title)


def messages(root, conversation_id):
    """Recover the selected conversation's messages in their original order.

    Every message carries the conversation identity. Filter by that identity and
    return persisted events, including unanswered user prompts after inference errors.
    """
    return [item for item in records(root, 'messages') if item['conversation_id'] == conversation_id]


def context_for(root, conversation_id):
    """Build model context from committed user and assistant messages.

    Comparison candidates are excluded until a separate selected-response message
    commits the human choice. Return only the role and content model APIs expect.
    """
    return [{'role': item['role'], 'content': item['content']} for item in messages(root, conversation_id)
            if item.get('role') in {'user', 'assistant'}]


def feedback(root, response_id, *, rating=None, binary=None, explanation='', correction='', preferred=None):
    """Attach a new human judgement to an exact saved response.

    Validate ratings and preference identity, then copy the response's original
    prompt context and generation configuration into an immutable feedback event.
    A correction creates an SFT example; an explicit candidate choice creates a DPO
    pair. Explanations and scalar scores remain metadata. Earlier feedback survives.
    """
    response = next((item for item in records(root, 'messages') if item['id'] == response_id), None)
    if response is None or response['role'] not in {'assistant', 'comparison'}:
        raise ValueError('Select a saved model response')
    if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
        raise ValueError('Rating must be an integer from 1 through 5')
    if binary is not None and type(binary) is not bool:
        raise ValueError('Binary feedback must be true or false')
    if preferred is not None and (response['role'] != 'comparison' or type(preferred) is not int or preferred not in (0, 1)):
        raise ValueError('Preference must choose candidate A or B')
    if preferred is not None and response['candidates'][0] == response['candidates'][1]:
        raise ValueError('Identical responses cannot form a preference pair; regenerate')
    return append(root, 'feedback', response_id=response_id,
                  response_link=f"[[messages/{response_id}]]", conversation_id=response['conversation_id'],
                  context=copy.deepcopy(response['context']), configuration=copy.deepcopy(response['configuration']),
                  rating=rating, binary=binary, explanation=explanation, correction=correction,
                  preferred=preferred, candidates=response.get('candidates'), original=response['content'])


def examples(root, algorithm, model, replay=False):
    """Reconstruct eligible examples for one method and exact base model.

    Read immutable feedback and exclude examples consumed by successful runs unless
    replay was requested. Corrections and A/B choices are separate datasets; ratings
    never become preferences. Keep event identifiers alongside each reconstructed row
    so a training run can snapshot its precise provenance.
    """
    if algorithm not in {'SFT', 'DPO'}:
        raise ValueError('Choose SFT or DPO')
    consumed = set()
    succeeded = {item['run_id'] for item in records(root, 'events') if item.get('status') == 'completed'}
    for run in records(root, 'training'):
        if run['id'] in succeeded and run['algorithm'] == algorithm and not replay:
            consumed.update(run['example_ids'])
    result = []
    for event in records(root, 'feedback'):
        if event['configuration']['model'] != model or event['id'] in consumed:
            continue
        row = None
        if algorithm == 'SFT' and event.get('correction', '').strip():
            row = {'prompt': event['context'], 'completion': [{'role': 'assistant', 'content': event['correction']}]}
        if algorithm == 'DPO' and event.get('preferred') is not None:
            chosen = event['preferred']
            row = {'prompt': event['context'],
                   'chosen': [{'role': 'assistant', 'content': event['candidates'][chosen]}],
                   'rejected': [{'role': 'assistant', 'content': event['candidates'][1 - chosen]}]}
        if row:
            result.append({'id': event['id'], 'row': row})
    return result


def notifications(root, model, threshold=10):
    """Count new SFT corrections and DPO preferences separately.

    The positive threshold controls notifications only. This function never loads
    a model, launches a worker, or starts training, even when counts exceed it.
    """
    if threshold < 1:
        raise ValueError('Feedback threshold must be positive')
    return {method: {'count': len(examples(root, method, model)),
                     'ready': len(examples(root, method, model)) >= threshold} for method in ('SFT', 'DPO')}
