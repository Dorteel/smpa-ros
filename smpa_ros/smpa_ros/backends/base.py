"""Cognitive stage settings and compatibility exports for shared generation controls."""
from smpa_llm.inference.backends.base import (Backend, GENERATION_KEYS, generation_capabilities,
    validate_generation, generation_arguments, validate_output)

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
