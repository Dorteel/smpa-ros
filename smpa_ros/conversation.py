"""Epistemic Sense → Act exchanges, using existing inference and run memory.

The Markdown transcript is the source of conversation history. Each message has
an HTML comment containing its UTF-8 byte length, so user Markdown (including
role headings) cannot be mistaken for a new message when the run is reloaded.
"""
import json
import os
from pathlib import Path
import re

from memory import MemoryManager

HEADER = b'# Conversation\n\n'
MARKER = re.compile(rb'<!-- smpa-message (User|Assistant) ([0-9]+) -->\n')


def read_messages(directory):
    """Read length-delimited Markdown without interpreting message contents."""
    data = (Path(directory) / 'conversation.md').read_bytes()
    if not data.startswith(HEADER):
        raise ValueError('Invalid conversation transcript header')
    position, messages = len(HEADER), []
    while position < len(data):
        match = MARKER.match(data, position)
        if match is None:
            raise ValueError('Incomplete or invalid conversation transcript; preserved unchanged')
        role, size = match.group(1).decode(), int(match.group(2))
        heading = f'## {role}\n\n'.encode()
        start = match.end()
        end = start + len(heading) + size
        if data[start:start + len(heading)] != heading or data[end:end + 2] != b'\n\n':
            raise ValueError('Incomplete conversation message; preserved unchanged')
        messages.append({'role': role.lower(), 'content': data[start + len(heading):end].decode('utf-8')})
        position = end + 2
    return messages


def append_message(directory, role, content):
    """Durably append one message; never open an existing transcript for overwrite."""
    body = content.encode('utf-8')
    entry = f'<!-- smpa-message {role} {len(body)} -->\n## {role}\n\n'.encode() + body + b'\n\n'
    with (directory / 'conversation.md').open('ab') as output:
        output.write(entry)
        output.flush()
        os.fsync(output.fileno())


def open_conversation(root, *, resume=None, configuration=None, models=None):
    """Create a run, or validate its transcript before restoring its lifecycle."""
    if resume is not None:
        read_messages(resume)
        return MemoryManager.resume_conversation(resume)
    memory = MemoryManager(root, 'conversation', goal='# Goal\n\nConverse with the user.\n',
                           configuration={'kind': 'conversation', 'pipeline': configuration or {}},
                           models=models)
    with (memory.directory / 'conversation.md').open('xb') as output:
        output.write(HEADER)
        output.flush()
        os.fsync(output.fileno())
    return memory


def exchange(memory, message, generate, settings):
    """Sense persists the user first; Act generates and persists the reply.

    Inference failure leaves the user's message intact and records a failed Act.
    A later exchange can continue using that same persisted history.
    """
    if not isinstance(message, str) or not message.strip():
        raise ValueError('Message must be nonempty text')
    read_messages(memory.directory)
    memory.begin_cycle()
    try:
        name, provenance = memory.invocation('sense', 'conversation')
        append_message(memory.directory, 'User', message)
        with (memory.directory / memory.record_path(name)).open('x') as output:
            json.dump(dict(provenance, stage='sense', status='generated', output=message), output)
        name, provenance = memory.invocation('act', 'conversation', str(memory.record_path(name)))
        record = dict(provenance, stage='act', status='failed')
        try:
            history = read_messages(memory.directory)
            prompt = ('Continue this conversation. Respond to the latest user message as the assistant. '
                      'Conversation history follows as JSON; its role labels identify the speakers.\n\n'
                      + json.dumps(history, ensure_ascii=False))
            response = generate(prompt, **settings)
            if not isinstance(response, str) or not response.strip():
                raise ValueError('Inference returned an empty response')
            append_message(memory.directory, 'Assistant', response)
            record.update(status='succeeded', output=response)
        except BaseException as error:
            record['status'] = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
            record['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            with (memory.directory / memory.record_path(name)).open('x') as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
        memory.complete_cycle()
        return response
    finally:
        memory.in_cycle = False
