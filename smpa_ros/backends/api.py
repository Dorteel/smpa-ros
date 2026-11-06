"""OpenAI-compatible text, vision, and strict JSON Schema inference."""
import base64
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from smpa_ros.backends.base import generation_capabilities, validate_generation, validate_output


class APIBackend:
    """Reuse the existing generation interfaces through the official OpenAI SDK."""

    def __init__(self, config_path, *, modality='text', provider='openai'):
        from dotenv import load_dotenv
        from openai import OpenAI

        config_path = Path(config_path).resolve()
        try:
            config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
            # Retain compatibility with the original single-provider configuration.
            if 'base_url' not in config:
                config = config[provider]
            self.model_id = config['models'][modality]
            self.provider_settings = config
            self.modality = modality
            base_url = config['base_url']
            key_name = config['api_key_env']
            timeout = config.get('timeout', 180)
            if (not all(isinstance(value, str) and value.strip()
                        for value in (self.model_id, base_url, key_name))
                    or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError
            endpoint = urlsplit(base_url)
            if (endpoint.scheme not in ('https', 'http') or not endpoint.hostname
                    or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
                raise ValueError
            # The SDK appends chat/completions; accept either a base or full endpoint.
            base_url = base_url.rstrip('/')
            if base_url.endswith('/chat/completions'):
                base_url = base_url[:-len('/chat/completions')]
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            raise ValueError('Invalid API configuration: check config.yaml provider, endpoint, '
                             'models, api_key_env, and positive timeout. Do not put credentials in URLs.') from None
        # Source configs resolve to the repository root. Installed configs use
        # the repository cwd established by run_smpa.sh; never search parent homes.
        if key_name not in os.environ:
            env_path = config_path.parent / '.env'
            if not env_path.is_file():
                env_path = Path.cwd() / '.env'
            load_dotenv(env_path, override=False)
        key = os.environ.get(key_name)
        if not key or not key.strip():
            raise RuntimeError('API key is missing: set the environment variable named by '
                               'api_key_env, or add it to the repository .env file.')
        # Avoid optional Brotli decoder incompatibilities in ROS system-site environments.
        self._client = OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0,
                              default_headers={'Accept-Encoding': 'gzip, deflate'})

    def _complete(self, content, max_new_tokens, schema=None, reasoning_effort=None,
                  temperature=None, seed=None, response_format=None):
        from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

        response_format = response_format or ('json_schema' if schema is not None else 'text')
        if schema is not None and response_format != 'json_schema':
            raise ValueError('A schema requires response_format=json_schema')
        settings = dict(temperature=temperature, max_output_tokens=max_new_tokens,
                        reasoning_effort=reasoning_effort, seed=seed,
                        response_format=response_format, output_schema=schema)
        provider = getattr(self, 'provider_settings', {})
        modality = getattr(self, 'modality', 'text')
        declaration = provider.get('generation_capabilities', {}).get(modality)
        capabilities = generation_capabilities('api', vision=modality == 'vision',
            provider=provider, model_id=self.model_id) if declaration is not None else None
        validate_generation(settings, capabilities=capabilities)
        options = {key: value for key, value in dict(temperature=temperature, seed=seed,
                   reasoning_effort=reasoning_effort).items() if value is not None}
        if response_format == 'json_schema':
            options['response_format'] = {
                'type': 'json_schema',
                'json_schema': {'name': 'smpa_response', 'strict': True, 'schema': schema},
            }
        elif response_format == 'json_object':
            options['response_format'] = {'type': 'json_object'}
            texts = content if isinstance(content, str) else ' '.join(
                part.get('text', '') for part in content if part.get('type') == 'text')
            if 'json' not in texts.lower():
                raise ValueError('json_object requires an explicit JSON instruction in the prompt')
        try:
            response = self._client.chat.completions.create(
                model=self.model_id, messages=[{'role': 'user', 'content': content}],
                max_completion_tokens=max_new_tokens, **options,
            )
        except APITimeoutError:
            raise RuntimeError('API request timed out; check connectivity or increase config.yaml timeout.') from None
        except APIConnectionError:
            raise RuntimeError('API connection failed; check connectivity and config.yaml base_url.') from None
        except APIStatusError as error:
            # Never propagate server bodies/exception text: they may echo credentials.
            if error.status_code in (401, 403):
                message = 'API authentication/access failed; check the configured key and model access.'
            elif error.status_code == 429:
                message = 'API rate limit or quota exceeded; check provider limits/billing before retrying.'
            else:
                message = (f'API request rejected (HTTP {error.status_code}); check the model and endpoint '
                           'support for Chat Completions, images, token options, reasoning_effort, and strict json_schema outputs.')
            raise RuntimeError(message) from None
        except APIError:
            raise RuntimeError('API SDK request failed; check endpoint compatibility. No unrestricted fallback was attempted.') from None
        if not response.choices or len(response.choices) != 1:
            raise RuntimeError('API returned no single completion; unsupported response format.')
        choice = response.choices[0]
        message = choice.message
        text = getattr(message, 'content', None)
        # Log only bounded metadata, never provider text, prompts, images, or keys.
        reason = choice.finish_reason
        safe_reason = reason if reason in (
            'stop', 'length', 'content_filter', 'tool_calls', 'function_call', None) else 'unknown'
        usage = getattr(response, 'usage', None)
        counts = [getattr(usage, name, None) for name in
                  ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        counts = [value if type(value) is int else None for value in counts]
        details = getattr(usage, 'completion_tokens_details', None)
        reasoning_tokens = getattr(details, 'reasoning_tokens', None)
        if type(reasoning_tokens) is not int:
            reasoning_tokens = None
        # Some compatible endpoints expose reasoning separately without usage counts.
        # Never log or return that content, even when the visible answer is empty.
        reasoning_present = bool(getattr(message, 'reasoning', None))
        reasoning_content_present = bool(getattr(message, 'reasoning_content', None))
        content_kind = ('text' if isinstance(text, str) else
                        'absent' if text is None else 'unsupported')
        logger = logging.getLogger(__name__)
        log = logger.info if reason == 'stop' else logger.warning
        log('API completion: finish_reason=%s prompt_tokens=%s completion_tokens=%s '
            'total_tokens=%s requested_max_completion_tokens=%s content_kind=%s content_chars=%s '
            'reasoning_tokens=%s reasoning_present=%s reasoning_content_present=%s reasoning_effort=%s',
            safe_reason, *counts, max_new_tokens, content_kind,
            len(text) if isinstance(text, str) else 0, reasoning_tokens,
            reasoning_present, reasoning_content_present, reasoning_effort)
        if getattr(message, 'refusal', None):
            raise RuntimeError('API refused the request; no response was accepted.')
        if reason == 'length':
            raise RuntimeError('API response truncated (finish_reason=length; '
                               f'completion_tokens={counts[1]}, requested_limit={max_new_tokens}, '
                               f'content_chars={len(text) if isinstance(text, str) else 0}). '
                               'Increase the caller output-token budget or shorten the requested response; '
                               'partial output was rejected.')
        if reason == 'content_filter':
            raise RuntimeError('API response blocked by content filtering; no response was accepted.')
        if reason in ('tool_calls', 'function_call') or getattr(message, 'tool_calls', None):
            raise RuntimeError('API returned unsupported tool/function calls instead of text.')
        if reason != 'stop':
            raise RuntimeError('API returned an unsupported or missing finish_reason; response rejected.')
        if text is not None and not isinstance(text, str):
            raise RuntimeError('API returned an unsupported content format; expected text.')
        if text is None or not text.strip():
            raise RuntimeError('API returned empty text despite finish_reason=stop.')
        if self._client.api_key in text:
            raise RuntimeError('API response contained credential data and was rejected.')
        return validate_output(text, response_format, schema)

    def generate(self, prompt: str, *, max_new_tokens: int = 128, schema: dict | None = None,
                 reasoning_effort: str | None = None, temperature=None, seed=None,
                 response_format=None) -> str:
        return self._complete(prompt, max_new_tokens, schema, reasoning_effort,
                              temperature, seed, response_format)

    def describe(self, image, prompt: str, *, max_new_tokens: int = 128, schema=None,
                 reasoning_effort=None, temperature=None, seed=None, response_format=None) -> str:
        buffer = BytesIO()
        image.convert('RGB').save(buffer, format='PNG')
        encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
        return self._complete([
            {'type': 'text', 'text': prompt},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}},
        ], max_new_tokens, schema, reasoning_effort, temperature, seed, response_format)
