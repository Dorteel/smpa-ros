import json
from urllib.request import Request, urlopen


class OllamaBackend:
    """Use an already provisioned model through the local Ollama server."""

    def __init__(self, model: str, url: str = 'http://localhost:11434',
                 timeout: float = 120):
        self.model = model
        self.url = url.rstrip('/') + '/api/generate'
        self.timeout = timeout

    def _complete(self, prompt: str, *, max_new_tokens: int, conversation=False):
        """Use the same request for text-only and structured conversational results."""
        payload = {
            'model': self.model, 'prompt': prompt, 'stream': False,
            'options': {'temperature': 0, 'seed': 0, 'num_predict': max_new_tokens},
        }
        request = Request(self.url, data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)
        if 'error' in result:
            raise RuntimeError(result['error'])
        if conversation:
            output = {'response': result['response']}
            if isinstance(result.get('thinking'), str) and result['thinking'].strip():
                output.update(thinking=result['thinking'], reasoning_source='thinking')
            if isinstance(result.get('model'), str) and result['model']:
                output['model_id'] = result['model']
            return output
        return result['response']

    def generate(self, prompt: str, *, max_new_tokens: int = 128) -> str:
        return self._complete(prompt, max_new_tokens=max_new_tokens)

    def generate_conversation(self, prompt: str, *, max_new_tokens: int = 128) -> dict:
        """Preserve an explicitly returned Ollama thinking field, if available."""
        return self._complete(prompt, max_new_tokens=max_new_tokens, conversation=True)
