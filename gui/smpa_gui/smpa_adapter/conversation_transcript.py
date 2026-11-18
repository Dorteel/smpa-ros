"""Read an SMPA conversation transcript (``conversation.md``) for display, without changing it.

Concepts:
    SMPA appends each message to the transcript as a length-delimited
    Markdown entry: a marker comment ``<!-- smpa-message <Role> <size>
    [agent <size>] [parts <thinking size> <response size>] -->``, an optional
    ``<!-- smpa-agent {...} -->`` provenance comment, an anchor
    ``<a id="message-<n>"></a>``, a ``## <Role>`` heading, the body and a
    blank line. Sizes are bytes, so message text is never interpreted.

    This reader is for showing conversations that the conversation server
    is not currently serving, or while it is busy. Unlike SMPA's own reader,
    it does not refuse an incomplete last entry (a writer may be midway);
    it returns the complete messages and says the rest is incomplete. The
    format belongs to SMPA; if it changes, this reader must follow.
"""
import hashlib
import json
from pathlib import Path
import re

HEADER = b'# Conversation\n\n'
MARKER = re.compile(rb'<!-- smpa-message (User|Assistant) ([0-9]+)(?: agent ([0-9]+))?(?: parts ([0-9]+) ([0-9]+))? -->\n')
ANCHOR = re.compile(rb'<a id="message-([0-9]+)"></a>\n')


def agent_summary(agent):
    """Keep the parts of a message's agent provenance that a reader needs."""
    if not isinstance(agent, dict):
        return None
    return {key: agent.get(key) for key in ('source', 'label', 'provider', 'model_id', 'backend', 'model_revision')}


def split_parts(body, thinking_size, response_size):
    """Separate the thinking and response sections of a versioned assistant message."""
    offset, thinking = 0, None
    if thinking_size:
        prefix, suffix = b'<thinking>\n', b'\n</thinking>\n\n'
        stop = len(prefix) + thinking_size
        if not body.startswith(prefix) or body[stop:stop + len(suffix)] != suffix:
            raise ValueError('invalid thinking section')
        thinking = body[len(prefix):stop].decode('utf-8')
        offset = stop + len(suffix)
    prefix = b'<response>\n'
    stop = offset + len(prefix) + response_size
    if body[offset:offset + len(prefix)] != prefix or body[stop:] != b'\n</response>':
        raise ValueError('invalid response section')
    return thinking, body[offset + len(prefix):stop].decode('utf-8')


def parse(data):
    """Parse transcript bytes into messages; return ``(messages, problem)``.

    ``problem`` is None for a complete transcript, otherwise a sentence about
    where reading stopped. Messages have ``index`` (1-based), ``role``
    (``user`` or ``assistant``), ``content``, ``thinking`` and ``agent``.
    """
    if not data.startswith(HEADER):
        return [], 'The file does not start with the conversation header.'
    position, messages = len(HEADER), []
    while position < len(data):
        match = MARKER.match(data, position)
        if match is None:
            return messages, f'Unreadable entry after message {len(messages)}.'
        role, size = match.group(1).decode(), int(match.group(2))
        start, agent = match.end(), None
        try:
            if match.group(3):
                end = start + int(match.group(3))
                text = data[start:end].decode('utf-8')
                agent = json.loads(text.removeprefix('<!-- smpa-agent ').removesuffix(' -->'))
                if data[end:end + 1] != b'\n':
                    raise ValueError('incomplete agent provenance')
                start = end + 1
            anchor = ANCHOR.match(data, start)
            if anchor:
                start = anchor.end()
            heading = f'## {role}\n\n'.encode()
            end = start + len(heading) + size
            if data[start:start + len(heading)] != heading or data[end:end + 2] != b'\n\n':
                raise ValueError('incomplete message')
            body = data[start + len(heading):end]
            thinking = None
            if match.group(4) is not None:
                thinking, content = split_parts(body, int(match.group(4)), int(match.group(5)))
            else:
                content = body.decode('utf-8')
        except (ValueError, UnicodeDecodeError) as error:
            return messages, f'Message {len(messages) + 1} is incomplete or invalid ({error}); a writer may be midway.'
        messages.append(dict(index=len(messages) + 1, role=role.lower(), content=content, thinking=thinking,
                             agent=agent_summary(agent)))
        position = end + 2
    return messages, None


def read(path):
    """Read a transcript file and return its state, revision and messages.

    State is ``ok``, ``missing``, ``unavailable`` (cannot be read) or
    ``partial`` (some messages, then an incomplete or invalid entry).
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return dict(state='missing', path=str(path), revision=None, messages=[], problem=None)
    except OSError as error:
        return dict(state='unavailable', path=str(path), revision=None, messages=[],
                    problem=f'{type(error).__name__}: {error.strerror or error}')
    messages, problem = parse(data)
    return dict(state='partial' if problem else 'ok', path=str(path), revision=hashlib.sha1(data).hexdigest()[:16],
                messages=messages, problem=problem)
