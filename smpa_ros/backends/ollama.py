import json
from urllib.request import Request, urlopen


class OllamaBackend:
    """Use an already provisioned model through the local Ollama server."""

    def __init__(self, model: str, url: str = 'http://localhost:11434',
                 timeout: float = 120):
        self.model = model
        self.url = url.rstrip('/') + '/api/generate'
        self.timeout = timeout

    def _complete(self, prompt: str | list[dict], *, max_new_tokens: int, conversation=False):
        """Use the same request for text-only and structured conversational results."""
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

    def generate(self, prompt: str, *, max_new_tokens: int = 128) -> str:
        return self._complete(prompt, max_new_tokens=max_new_tokens)

    def generate_conversation(self, prompt: str | list[dict], *, max_new_tokens: int = 128) -> dict:
        """Preserve an explicitly returned Ollama thinking field, if available."""
        return self._complete(prompt, max_new_tokens=max_new_tokens, conversation=True)
