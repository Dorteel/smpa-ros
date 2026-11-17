"""Validate interactive generation controls without accessing cognitive state."""
import math

# The same validation applies to HTTP overrides and direct backend calls.
GENERATION_FIELDS = {'max_new_tokens', 'temperature', 'top_p', 'top_k',
                     'repetition_penalty', 'seed', 'do_sample', 'enable_thinking'}


def validate_generation(values, supported):
    """Return a validated copy of interactive generation controls.

    Values are the requested overrides; supported names describe the chosen
    backend. Reject unknown controls, invalid types, nonfinite values and
    out-of-range values before generation. Sampling requires a positive
    temperature. Return a new dictionary without changing either input.
    """
    if not isinstance(values, dict):
        raise ValueError('Generation settings must be an object')
    unknown = set(values) - set(supported)
    if unknown:
        raise ValueError('Unsupported generation settings: ' + ', '.join(sorted(unknown)))
    for name, value in values.items():
        if name in ('do_sample', 'enable_thinking'):
            valid = type(value) is bool
        elif name in ('max_new_tokens', 'top_k', 'seed'):
            low, high = {'max_new_tokens': (1, 32768), 'top_k': (0, 100000),
                         'seed': (0, 4294967295)}[name]
            valid = type(value) is int and low <= value <= high
        elif name == 'reasoning_effort':
            valid = value in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')
        else:
            low, high = {'temperature': (0, 2), 'top_p': (0, 1),
                         'repetition_penalty': (0, 10)}[name]
            valid = (type(value) in (int, float) and math.isfinite(value)
                     and low <= value <= high and (name == 'temperature' or value > 0))
        if not valid:
            raise ValueError(f'Invalid generation setting: {name}')
    if values.get('do_sample') and values.get('temperature', 1) <= 0:
        raise ValueError('Sampling requires temperature greater than zero')
    return dict(values)

