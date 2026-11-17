from typing import Protocol


class Backend(Protocol):
    """The text inference contract shared by both backends."""

    def generate(self, prompt: str, *, max_new_tokens: int = 128, temperature=None,
                 reasoning_effort=None, seed=None, response_format=None, schema=None) -> str:
        ...


GENERATION_KEYS = ('temperature', 'max_output_tokens', 'reasoning_effort', 'seed', 'response_format')


def stage_generation(configuration, stage):
    """Resolve legacy overrides without storing duplicate editable definitions."""
    from copy import deepcopy
    sections = {'sense': 'vision', 'model': 'modeling', 'plan': 'planning'}
    raw = deepcopy(configuration.get('stage_settings', {}).get(stage, {}))
    if stage == 'act':
        # max_new_tokens was formerly returned as unused Act provenance.
        raw.pop('max_new_tokens', None)
        result = dict.fromkeys(GENERATION_KEYS)
        result.update(raw)
        validate_generation(result, backend='act')
        return result
    section = configuration.get(sections[stage], {})
    shared = configuration.get('model')
    default_backend = (shared.get('backend', 'transformers') if isinstance(shared, dict) else
                       configuration.get('modeling', {}).get('backend', 'transformers') if stage == 'plan' else 'transformers')
    backend = section.get('backend', default_backend)
    if 'max_new_tokens' in raw and (type(raw['max_new_tokens']) is not int or raw['max_new_tokens'] <= 0):
        raise ValueError('max_new_tokens must be a positive integer')
    legacy_tokens = raw.pop('max_new_tokens', None)
    default_tokens = {'sense': 512 if backend == 'api' else 128, 'model': 512, 'plan': 128}[stage]
    canonical = bool(set(raw) & set(GENERATION_KEYS))
    result = dict(temperature=None if canonical else 0, max_output_tokens=legacy_tokens or default_tokens,
                  reasoning_effort=None, seed=0 if backend == 'ollama' and not canonical else None,
                  response_format='json_schema' if stage == 'plan' else 'text')
    result.update(raw)
    # Historical section overrides retain their precedence, including explicit nulls.
    for key in (*GENERATION_KEYS, 'output_schema'):
        if key in section:
            result[key] = deepcopy(section[key])
    if stage == 'plan':
        from smpa_ros.plan import ACTION_SCHEMA, action_schema, enabled_actions
        result.setdefault('output_schema', deepcopy(ACTION_SCHEMA))
        if result['response_format'] != 'json_schema':
            raise ValueError('Plan requires json_schema and its fixed action contract')
        validate_action_schema(result['output_schema'])
        result['output_schema'] = action_schema(enabled_actions(configuration))
    validate_generation(result)
    return result


def generation_capabilities(backend, *, vision=False, provider=None, model_id=None):
    """Supported controls, using explicit declarations for configured API models."""
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
    """Keep the existing backend token argument while exposing canonical config keys."""
    validate_generation(settings)
    result = {key: value for key, value in settings.items()
              if value is not None and key not in ('max_output_tokens', 'output_schema')}
    result['max_new_tokens'] = settings['max_output_tokens']
    if settings.get('response_format') == 'json_schema':
        result['schema'] = settings['output_schema']
    return result


def validate_output(text, response_format='text', schema=None):
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


def validate_action_schema(schema, actions=None):
    """Annotations may change; the ROS action contract cannot."""
    from smpa_ros.plan import ACTION_SCHEMA, AVAILABLE_ACTIONS, action_schema
    def contract(value):
        if isinstance(value, dict):
            return {key: contract(item) for key, item in value.items()
                    if key not in ('title', 'description', '$comment')}
        if isinstance(value, list):
            return [contract(item) for item in value]
        return value
    validate_generation(dict(response_format='json_schema', output_schema=schema))
    names = schema.get('properties', {}).get('action', {}).get('enum', []) if isinstance(schema, dict) else []
    valid_names = isinstance(names, list) and bool(names) and all(isinstance(name, str) and name in AVAILABLE_ACTIONS for name in names) and len(set(names)) == len(names)
    expected = action_schema(actions if actions is not None else names)
    if contract(schema) != ACTION_SCHEMA and (not valid_names or contract(schema) != expected):
        raise ValueError('Plan schema must preserve the fixed ROS action contract')
