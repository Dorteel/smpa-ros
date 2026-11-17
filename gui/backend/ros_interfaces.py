"""Describe ROS 2 message types and check JSON values against them.

Concepts:
    A schema is plain data describing one message type: every field with its
    kind (boolean, integer, float, string, array or nested message) and the
    limits ROS imposes on it, such as integer ranges, string bounds and
    array sizes. Schemas are derived from the installed interface
    definitions at run time, so no action or message is known in advance.

    Validation compares a JSON value with a schema and collects every
    disagreement as a path and an explanation. Only a value without
    disagreements is turned into a ROS message.
"""
import math

from backend.gateway import GatewayError

INTEGER_RANGES = {
    'int8': (-2 ** 7, 2 ** 7 - 1), 'uint8': (0, 2 ** 8 - 1), 'int16': (-2 ** 15, 2 ** 15 - 1),
    'uint16': (0, 2 ** 16 - 1), 'int32': (-2 ** 31, 2 ** 31 - 1), 'uint32': (0, 2 ** 32 - 1),
    'int64': (-2 ** 63, 2 ** 63 - 1), 'uint64': (0, 2 ** 64 - 1),
    'octet': (0, 255), 'byte': (0, 255), 'char': (0, 255), 'wchar': (0, 2 ** 16 - 1),
}
FLOAT_LIMITS = {'float': 3.4028234663852886e38, 'double': None, 'long double': None}


class InvalidValue(ValueError):
    """A JSON value does not fit the message type; ``errors`` lists every disagreement."""

    def __init__(self, errors):
        """Keep the list of ``{'path', 'message'}`` entries for the caller to show."""
        super().__init__('; '.join(f"{error['path'] or 'goal'}: {error['message']}" for error in errors))
        self.errors = errors


def action_class(type_name):
    """Find the Python class of an action type such as ``example_interfaces/action/Fibonacci``.

    The class comes from the ROS environment this process was started in. A
    type whose package is not sourced cannot be used; that is reported as a
    ``GatewayError`` naming the type.
    """
    from rosidl_runtime_py.utilities import get_action
    try:
        return get_action(type_name)
    except (AttributeError, ModuleNotFoundError, ValueError) as error:
        raise GatewayError(f'Action type {type_name} is not available in this ROS environment '
                           f'(source the workspace that defines it): {error}') from error


def describe_type(slot_type):
    """Describe one field type from the interface definition as schema data.

    Basic types become boolean, integer (with range) or float (with limit).
    Strings carry their maximum length when bounded. Arrays and sequences
    carry their fixed or maximum size and the schema of their elements.
    Nested messages are described recursively through their own class.
    """
    from rosidl_parser import definition
    from rosidl_runtime_py.import_message import import_message_from_namespaced_type

    if isinstance(slot_type, definition.BasicType):
        name = slot_type.typename
        if name == 'boolean':
            return dict(kind='boolean', type=name)
        if name in INTEGER_RANGES:
            low, high = INTEGER_RANGES[name]
            return dict(kind='integer', type=name, minimum=low, maximum=high)
        return dict(kind='float', type=name, limit=FLOAT_LIMITS.get(name))
    if isinstance(slot_type, definition.AbstractGenericString):
        return dict(kind='string', type='wstring' if isinstance(slot_type, definition.AbstractWString) else 'string',
                    max_length=getattr(slot_type, 'maximum_size', None))
    if isinstance(slot_type, definition.AbstractNestedType):
        return dict(kind='array', item=describe_type(slot_type.value_type),
                    fixed_size=slot_type.size if isinstance(slot_type, definition.Array) else None,
                    max_size=getattr(slot_type, 'maximum_size', None))
    if isinstance(slot_type, definition.NamespacedType):
        return describe(import_message_from_namespaced_type(slot_type))
    raise GatewayError(f'Unsupported interface field type: {slot_type!r}')


def describe(message_class):
    """Describe a message class as a schema listing its fields in definition order."""
    module = message_class.__module__.split('.')
    names = list(message_class.get_fields_and_field_types())
    return dict(kind='message', type=f'{module[0]}/{module[1]}/{message_class.__name__}',
                fields=[dict(name=name, schema=describe_type(slot_type))
                        for name, slot_type in zip(names, message_class.SLOT_TYPES)])


def to_json(value):
    """Convert a ROS message or field value to complete JSON data, without shortening.

    Used for default values and submitted goals, which must round-trip
    exactly. Non-finite floats become ``None`` because JSON cannot express
    them. Byte strings become lists of integers.
    """
    if hasattr(value, 'get_fields_and_field_types'):
        return {name: to_json(getattr(value, name)) for name in value.get_fields_and_field_types()}
    if isinstance(value, (bool, str)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if hasattr(value, 'tolist'):
        return to_json(value.tolist())
    if hasattr(value, '__len__'):
        return [to_json(item) for item in value]
    return str(value)


def defaults(message_class):
    """Return the default field values the interface definition gives a new message."""
    return to_json(message_class())


def check(schema, value, path, errors):
    """Compare one JSON value with its schema and record every disagreement.

    Booleans must be true or false, not numbers. Integers must be whole
    numbers within the type's range. Floats must be finite numbers within the
    type's limit. Strings must respect a bound. Arrays must have the fixed
    size or stay within the maximum, and each element is checked. Messages
    must be objects containing only known fields; missing fields keep their
    defaults. Return the value in the form the message setters accept.
    """
    kind = schema['kind']
    if kind == 'boolean':
        if not isinstance(value, bool):
            errors.append(dict(path=path, message='must be true or false'))
        return value
    if kind == 'integer':
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(dict(path=path, message=f"must be a whole number ({schema['type']})"))
        elif not schema['minimum'] <= value <= schema['maximum']:
            errors.append(dict(path=path, message=f"must be between {schema['minimum']} and {schema['maximum']} "
                                                  f"({schema['type']})"))
        return value
    if kind == 'float':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(dict(path=path, message=f"must be a number ({schema['type']})"))
            return value
        if not math.isfinite(value) or (schema['limit'] and abs(value) > schema['limit']):
            errors.append(dict(path=path, message=f"must be a finite number that fits {schema['type']}"))
        return float(value)
    if kind == 'string':
        if not isinstance(value, str):
            errors.append(dict(path=path, message='must be text'))
        elif schema['max_length'] is not None and len(value) > schema['max_length']:
            errors.append(dict(path=path, message=f"must have at most {schema['max_length']} characters"))
        return value
    if kind == 'array':
        if not isinstance(value, list):
            errors.append(dict(path=path, message='must be a list'))
            return value
        if schema['fixed_size'] is not None and len(value) != schema['fixed_size']:
            errors.append(dict(path=path, message=f"must have exactly {schema['fixed_size']} elements"))
        if schema['max_size'] is not None and len(value) > schema['max_size']:
            errors.append(dict(path=path, message=f"must have at most {schema['max_size']} elements"))
        return [check(schema['item'], item, f'{path}[{index}]', errors) for index, item in enumerate(value)]
    if not isinstance(value, dict):
        errors.append(dict(path=path, message=f"must be an object with the fields of {schema['type']}"))
        return value
    known = {field['name']: field['schema'] for field in schema['fields']}
    checked = {}
    for name, item in value.items():
        if name not in known:
            errors.append(dict(path=f'{path}.{name}'.lstrip('.'), message=f"is not a field of {schema['type']}"))
        else:
            checked[name] = check(known[name], item, f'{path}.{name}'.lstrip('.'), errors)
    return checked


def build(message_class, value):
    """Create a ROS message from a JSON value, or raise ``InvalidValue`` listing what is wrong.

    The value is checked completely first, so the message setters are only
    reached with data of the right types and sizes. Fields left out of the
    value keep the defaults of the interface definition.
    """
    from rosidl_runtime_py.set_message import set_message_fields

    errors = []
    checked = check(describe(message_class), value, '', errors)
    if errors:
        raise InvalidValue(errors)
    message = message_class()
    try:
        set_message_fields(message, checked)
    except Exception as error:  # a rule of the generated setters that the schema did not capture
        raise InvalidValue([dict(path='', message=f'was not accepted by the ROS message type: {error}')]) from error
    return message
