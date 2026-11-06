import json

from smpa_ros.backends.base import validate_generation, validate_output
from urllib.request import Request, urlopen


class OllamaBackend:
    """Use an already provisioned model through the local Ollama server."""

    def __init__(self, model: str, url: str = 'http://localhost:11434',
                 timeout: float = 120):
        self.model = model
        self.url = url.rstrip('/') + '/api/generate'
        self.timeout = timeout

    def generate(self, prompt: str, *, max_new_tokens: int = 128, temperature=None,
                 seed=None, reasoning_effort=None, response_format=None, schema=None) -> str:
        response_format = response_format or ('json_schema' if schema is not None else 'text')
        if schema is not None and response_format != 'json_schema':
            raise ValueError('A schema requires response_format=json_schema')
        validate_generation(dict(temperature=temperature, max_output_tokens=max_new_tokens,
            seed=seed, reasoning_effort=reasoning_effort, response_format=response_format,
            output_schema=schema), backend='ollama')
        payload = {
            'model': self.model, 'prompt': prompt, 'stream': False,
            'options': dict(num_predict=max_new_tokens, **{key: value for key, value in
                dict(temperature=temperature, seed=seed).items() if value is not None}),
        }
        if response_format != 'text':
            payload['format'] = schema if response_format == 'json_schema' else 'json'
        request = Request(self.url, data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)
        if 'error' in result:
            raise RuntimeError(result['error'])
        if result.get('done_reason') == 'length':
            raise RuntimeError('Ollama response was truncated; output rejected')
        return validate_output(result['response'], response_format, schema)
