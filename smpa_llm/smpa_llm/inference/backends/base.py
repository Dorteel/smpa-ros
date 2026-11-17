"""Backend contracts and generation validation independent of cognitive stages."""
from typing import Protocol

class Backend(Protocol):
    """The text inference contract shared by both backends."""

    def generate(self, prompt: str, *, max_new_tokens: int = 128, temperature=None,
                 reasoning_effort=None, seed=None, response_format=None, schema=None) -> str:
        """Return generated text with the caller’s existing controls.

        Implementations must enforce supplied output schemas and preserve the
        request budget. This protocol loads no model and defines no execution
        process or lifecycle.
        """
        ...

GENERATION_KEYS = ('temperature', 'max_output_tokens', 'reasoning_effort', 'seed', 'response_format')

def generation_capabilities(backend, *, vision=False, provider=None, model_id=None):
    """Describe generation controls supported by the selected backend and model.

    Act exposes no generation. Local engines retain their existing common
    controls; API engines expose only confirmed provider declarations for the
    selected modality and model. Return fresh lists of parameters, response
    formats and reasoning efforts without contacting a server.
    """
    if backend == 'act':
        return dict(parameters=[], response_formats=[], reasoning_efforts=[])
    parameters = ['temperature', 'max_output_tokens', 'seed']
    formats = ['text', 'json_object', 'json_schema']
    efforts = []
    if backend == 'api':
        # No universal model capability endpoint exists for compatible APIs.
        # Unknown models expose only the existing token-budget/text contract.
        parameters, formats = ['max_output_tokens'], ['text']
        modality = 'vision' if vision else 'text'
        provider = provider or {}
        declaration = provider.get('generation_capabilities', {}).get(modality, {})
        if model_id == provider.get('models', {}).get(modality):
            parameters = declaration.get('parameters', parameters)
            formats = declaration.get('response_formats', formats)
            efforts = declaration.get('reasoning_efforts', [])
    return dict(parameters=list(parameters), response_formats=list(formats), reasoning_efforts=list(efforts))

def validate_generation(settings, *, backend=None, capabilities=None):
    """Validate one request without changing its settings or loading a model.

    Reject unknown controls, invalid numeric ranges and unsafe schemas. Apply
    backend restrictions and declared capabilities when supplied. Successful
    validation returns normally; invalid requests raise ValueError.
    """
    import math
    unknown = set(settings) - {*GENERATION_KEYS, 'output_schema'}
    if unknown:
        raise ValueError('Unknown generation parameter')
    if backend == 'act':
        if any(value is not None for value in settings.values()):
            raise ValueError('Act generation parameters are unavailable: ROS action dispatch uses no inference')
        return
    temperature = settings.get('temperature')
    if temperature is not None and (type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError('temperature must be a finite number between 0 and 2, or null')
    limit = settings.get('max_output_tokens')
    if 'max_output_tokens' in settings and (type(limit) is not int or limit <= 0):
        raise ValueError('max_output_tokens must be a positive integer')
    seed = settings.get('seed')
    if seed is not None and (type(seed) is not int or not -(2**31) <= seed < 2**31):
        raise ValueError('seed must be a signed 32-bit integer or null (reproducibility is best-effort)')
    effort = settings.get('reasoning_effort')
    if effort is not None and effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
        raise ValueError('Unsupported reasoning_effort')
    response = settings.get('response_format') or 'text'
    if response not in ('text', 'json_object', 'json_schema'):
        raise ValueError('response_format must be text, json_object, or json_schema')
    if response == 'json_schema':
        from jsonschema.validators import validator_for
        schema = settings.get('output_schema')
        if not isinstance(schema, dict):
            raise ValueError('json_schema requires an output_schema object')
        try:
            validator_for(schema).check_schema(schema)
            if '$ref' in str(schema) or '$dynamicRef' in str(schema):
                # Never resolve external resources supplied through the GUI.
                def references(value):
                    """Walk nested schema values and reject nonlocal references.

                    Inspect dictionaries and lists recursively. Only fragment
                    references are permitted, so validation cannot fetch an
                    external resource supplied in a request.
                    """
                    if isinstance(value, dict):
                        for key, item in value.items():
                            if key in ('$ref', '$dynamicRef') and (not isinstance(item, str) or not item.startswith('#')):
                                raise ValueError('Only local schema references are supported')
                            references(item)
                    elif isinstance(value, list):
                        for item in value:
                            references(item)
                references(schema)
        except Exception:
            raise ValueError('Invalid JSON Schema; only local references are supported') from None
    if backend == 'transformers' and effort is not None:
        raise ValueError('reasoning_effort is unsupported by local Transformers inference')
    if backend == 'ollama' and effort is not None:
        raise ValueError('reasoning_effort is unavailable without verified model-specific Ollama thinking capabilities')
    if capabilities is not None:
        for key in ('temperature', 'max_output_tokens', 'reasoning_effort', 'seed'):
            if settings.get(key) is not None and key not in capabilities['parameters']:
                raise ValueError(f'{key} is unsupported or unconfirmed for the selected model/provider')
        if response not in capabilities['response_formats']:
            raise ValueError('response_format is unsupported or unconfirmed for the selected model/provider')
        if effort is not None and effort not in capabilities['reasoning_efforts']:
            raise ValueError('reasoning_effort is unsupported for the selected model/provider')

def generation_arguments(settings):
    """Convert validated canonical controls to the existing backend call arguments.

    Validate the supplied settings, omit unavailable optional values, and map
    max_output_tokens to max_new_tokens. Forward an output schema only for
    json_schema responses. Return a new argument dictionary without changing
    the caller’s configuration or weakening output validation.
    """
    validate_generation(settings)
    result = {key: value for key, value in settings.items()
              if value is not None and key not in ('max_output_tokens', 'output_schema')}
    result['max_new_tokens'] = settings['max_output_tokens']
    if settings.get('response_format') == 'json_schema':
        result['schema'] = settings['output_schema']
    return result

def validate_output(text, response_format='text', schema=None):
    """Return the original text only when it satisfies the requested contract.

    Plain text is returned unchanged. Structured output must parse as JSON and
    match its supplied schema, or be an object when no schema is supplied.
    Invalid output raises RuntimeError; no repair or retry is performed.
    """
    import json
    from jsonschema.validators import validator_for
    if response_format == 'text' and schema is None:
        return text
    try:
        value = json.loads(text)
        if schema is not None:
            validator_for(schema)(schema).validate(value)
        elif not isinstance(value, dict):
            raise ValueError
    except Exception:
        raise RuntimeError('Generation returned invalid JSON or output violating the selected schema') from None
    return text
