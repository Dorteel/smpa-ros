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

    def _complete(self, prompt: str | list[dict], *, max_new_tokens: int, conversation=False):
        """Send conversational turns without flattening roles or losing metadata.

        Use Ollama's chat endpoint for message lists and its generate endpoint for
        plain prompts. Reject server errors or truncation, then return the final
        response with explicitly supplied thinking and model identity when present.
        This path does not alter the robot generation interface or its controls.
        """
        validate_generation(dict(max_output_tokens=max_new_tokens), backend='ollama')
        chat = conversation and isinstance(prompt, list)
        payload = {
            'model': self.model, ('messages' if chat else 'prompt'): prompt, 'stream': False,
            'options': {'temperature': 0, 'seed': 0, 'num_predict': max_new_tokens},
        }
        url = self.url.removesuffix('/api/generate') + '/api/chat' if chat else self.url
        request = Request(url, data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)
        if 'error' in result:
            raise RuntimeError(result['error'])
        if result.get('done_reason') == 'length':
            raise RuntimeError('Ollama response was truncated; output rejected')
        if conversation:
            message = result['message'] if chat else result
            output = {'response': message['content' if chat else 'response'],
                      'effective_settings': dict(temperature=0, seed=0, max_new_tokens=max_new_tokens)}
            if isinstance(message.get('thinking'), str) and message['thinking'].strip():
                output.update(thinking=message['thinking'], reasoning_source='thinking')
            if isinstance(result.get('model'), str) and result['model']:
                output['model_id'] = result['model']
            return output
        return result['response']

    def generate_conversation(self, prompt: str | list[dict], *, max_new_tokens: int = 128) -> dict:
        """Preserve an explicitly returned Ollama thinking field, if available."""
        return self._complete(prompt, max_new_tokens=max_new_tokens, conversation=True)
