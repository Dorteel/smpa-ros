import json
from urllib.request import Request, urlopen


class OllamaBackend:
    """Use an already provisioned model through the local Ollama server."""

    def __init__(self, model: str, url: str = 'http://localhost:11434',
                 timeout: float = 120):
        self.model = model
        self.url = url.rstrip('/') + '/api/generate'
        self.timeout = timeout

    def generate(self, prompt: str, *, max_new_tokens: int = 128) -> str:
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
        return result['response']
