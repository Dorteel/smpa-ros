"""A scripted, local OpenAI-compatible Chat Completions endpoint for conversation tests.

Concepts:
    SMPA's conversation server talks to API providers through the official
    OpenAI SDK. Pointing a test provider at this stub exercises SMPA's real
    conversation code (persistence, resume, model selection) without model
    weights, credentials or network access. The stub is a test fixture only:
    its replies are canned and say so, and it never claims to be a model.

    Replies: a plain chat request is answered with ``Stub reply <n> to: <last
    user message>``. Structured requests from SMPA's belief modelling get a
    minimal valid JSON object. A user message containing ``FAIL-INFERENCE``
    makes the conversation's reply request fail with HTTP 500 (SMPA keeps the
    message). A message containing ``FAIL-BELIEF`` makes the first two belief
    requests about it fail with HTTP 500 (both belief updates of that
    exchange), which leaves SMPA's inference server in its error state until
    a model is loaded again.

    Usage: ``python openai_stub.py <port>``; prints ``STUB READY`` when listening.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import time

COUNTER = [0]
FAILED_BELIEF = []


def reply_text(body):
    """Choose the canned reply for one Chat Completions request body."""
    messages = body.get('messages', [])
    last = next((item for item in reversed(messages) if item.get('role') == 'user'), {})
    content = last.get('content', '')
    content = content if isinstance(content, str) else json.dumps(content)
    system = ' '.join(item.get('content', '') for item in messages if item.get('role') == 'system'
                      and isinstance(item.get('content'), str))
    belief = '"sds"' in system or 'triples' in system
    if 'FAIL-INFERENCE' in content and not belief:
        return None  # the conversation's reply fails; SMPA keeps the user's message
    if 'FAIL-BELIEF' in content and belief and len(FAILED_BELIEF) < 2:
        FAILED_BELIEF.append(content)
        return None  # both belief updates of that exchange fail; SMPA's inference server stays in its error state
    if '"sds"' in system:
        return json.dumps({'sds': 'The speaker said something.'})
    if 'triples' in system:
        return json.dumps({'triples': [{'s': '#stub_speaker', 'p': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type',
                                        'o': {'uri': 'urn:smpa:belief#Entity'}}]})
    COUNTER[0] += 1
    return f'Stub reply {COUNTER[0]} to: {content}'


class Handler(BaseHTTPRequestHandler):
    """Answer POST /v1/chat/completions in the OpenAI response format."""

    def do_POST(self):
        """Return one completion, or HTTP 500 when the scripted failure word is present."""
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        text = reply_text(body)
        if text is None:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "scripted failure"}}')
            return
        payload = dict(id='stub', object='chat.completion', created=int(time.time()), model=body.get('model', 'stub'),
                       choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content=text))],
                       usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        """Stay quiet; tests read results through SMPA's records."""


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)
    print('STUB READY', flush=True)
    server.serve_forever()
