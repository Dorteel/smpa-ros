"""Epistemic Sense → Act exchanges, using existing inference and run memory.

The Markdown transcript is the source of conversation history. Each message has
an HTML comment containing its UTF-8 byte length, so user Markdown (including
role headings) cannot be mistaken for a new message when the run is reloaded.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re

from memory import MemoryManager

HEADER = b'# Conversation\n\n'
MARKER = re.compile(rb'<!-- smpa-message (User|Assistant) ([0-9]+)(?: agent ([0-9]+))?(?: parts ([0-9]+) ([0-9]+))? -->\n')


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
        agent = None
        if match.group(3):
            agent_end = start + int(match.group(3))
            agent = json.loads(data[start:agent_end].decode().removeprefix('<!-- smpa-agent ').removesuffix(' -->'))
            if data[agent_end:agent_end + 1] != b'\n':
                raise ValueError('Incomplete agent provenance')
            start = agent_end + 1
        end = start + len(heading) + size
        if data[start:start + len(heading)] != heading or data[end:end + 2] != b'\n\n':
            raise ValueError('Incomplete conversation message; preserved unchanged')
        body = data[start + len(heading):end]
        thinking = None
        if match.group(4) is not None:
            # Only versioned entries use tags. Literal tags in legacy messages
            # remain ordinary text, and embedded closing tags cannot split parts.
            if role != 'Assistant':
                raise ValueError('Typed response parts require an assistant message')
            thinking_size, response_size = int(match.group(4)), int(match.group(5))
            offset = 0
            if thinking_size:
                prefix, suffix = b'<thinking>\n', b'\n</thinking>\n\n'
                stop = len(prefix) + thinking_size
                if not body.startswith(prefix) or body[stop:stop + len(suffix)] != suffix:
                    raise ValueError('Invalid thinking section')
                thinking = body[len(prefix):stop].decode('utf-8')
                offset = stop + len(suffix)
            prefix, suffix = b'<response>\n', b'\n</response>'
            stop = offset + len(prefix) + response_size
            if body[offset:offset + len(prefix)] != prefix or body[stop:] != suffix:
                raise ValueError('Invalid response section')
            content = body[offset + len(prefix):stop].decode('utf-8')
        else:
            content = body.decode('utf-8')
        message = {'role': role.lower(), 'content': content}
        if role == 'Assistant':
            message['response'] = content
            message['parts'] = []
            if thinking is not None:
                message['thinking'] = thinking
                message['parts'].append({'type': 'thinking', 'text': thinking})
            message['parts'].append({'type': 'response', 'text': content})
        else:
            message['parts'] = [{'type': 'user', 'text': content}]
        if agent is not None:
            message['agent'] = agent
        messages.append(message)
        position = end + 2
    return messages


def append_message(directory, role, content, *, agent=None, thinking=None, sections=False):
    """Durably append one message; never open an existing transcript for overwrite."""
    body = content.encode('utf-8')
    part_lengths = ''
    if sections:
        trace = thinking.encode('utf-8') if thinking else b''
        part_lengths = f' parts {len(trace)} {len(body)}'
        body = b'<response>\n' + body + b'\n</response>'
        if trace:
            body = b'<thinking>\n' + trace + b'\n</thinking>\n\n' + body
    # A length-delimited HTML comment keeps provenance out of the message body.
    metadata = (('<!-- smpa-agent ' + json.dumps(agent, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e')
                 + ' -->').encode() if agent is not None else b'')
    suffix = f' agent {len(metadata)}' if metadata else ''
    entry = f'<!-- smpa-message {role} {len(body)}{suffix}{part_lengths} -->\n'.encode()
    if metadata:
        entry += metadata + b'\n'
    entry += f'## {role}\n\n'.encode() + body + b'\n\n'
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


def exchange(memory, message, generate, settings, *, agent=None):
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
        if agent is not None:
            record['agent'] = agent
        try:
            history = [{key: item[key] for key in ('role', 'content')}
                       for item in read_messages(memory.directory)]
            prompt = ('Continue this conversation. Respond to the latest user message as the assistant. '
                      'Conversation history follows as JSON; its role labels identify the speakers.\n\n'
                      + json.dumps(history, ensure_ascii=False))
            result = generate(prompt, **settings)
            # String callbacks remain supported; they never imply a trace.
            result = {'response': result} if isinstance(result, str) else result
            if not isinstance(result, dict):
                raise ValueError('Inference must return text or a structured conversation result')
            response, thinking = result.get('response'), result.get('thinking')
            if not isinstance(response, str) or not response.strip():
                raise ValueError('Inference returned an empty response')
            if thinking is not None and not isinstance(thinking, str):
                raise ValueError('Reasoning must be explicitly returned text')
            if thinking is not None and not thinking.strip():
                thinking = None
            actual_agent = deepcopy(agent)
            reported_model = result.get('model_id')
            if actual_agent is not None and isinstance(reported_model, str) and reported_model:
                if reported_model != actual_agent['model_id']:
                    actual_agent['requested_model_id'] = actual_agent['model_id']
                    actual_agent['model_id'] = reported_model
                    kind = 'API' if actual_agent['backend'] == 'api' else 'Local'
                    actual_agent['label'] = f"{kind} · {actual_agent['provider']} · {reported_model}"
                record['agent'] = actual_agent
            append_message(memory.directory, 'Assistant', response, agent=actual_agent,
                           thinking=thinking, sections=True)
            record.update(status='succeeded', output=response, response=response)
            if thinking is not None:
                record['thinking'] = thinking
                record['reasoning_source'] = result.get('reasoning_source')
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
