"""Sense modalities: Hear acquires audio, Read observes text, Vision acquires images.

The robot stage remains a sequential Python call. Modality switches select
existing participants and do not create a second cognitive execution workflow.
"""


def modalities(config, service='vision'):
    """Resolve explicit modality switches while retaining legacy service defaults.

    The old vision/speech/all selection remains the default when sensing is
    absent. Explicit booleans override Hear and Vision independently. Read is
    opt-in and requires a selected conversation directory. Reject malformed
    switches before starting participants; an all-disabled result is permitted
    and represents an empty observation rather than a new execution mode.
    """
    settings = config.get('sensing', {})
    if not isinstance(settings, dict) or set(settings) - {'hear', 'read', 'vision'}:
        raise ValueError('sensing accepts only hear, read and vision settings')
    result = dict(hear=service in ('speech', 'all'), vision=service in ('vision', 'all'))
    for name in ('hear', 'vision'):
        value = settings.get(name, result[name])
        if type(value) is not bool:
            raise ValueError(f'sensing.{name} must be boolean')
        result[name] = value
    text = settings.get('read', {})
    if not isinstance(text, dict) or set(text) - {'enabled', 'session'}:
        raise ValueError('sensing.read accepts enabled and session')
    result['read'] = text.get('enabled', False)
    if type(result['read']) is not bool:
        raise ValueError('sensing.read.enabled must be boolean')
    if result['read'] and (not isinstance(text.get('session'), str) or not text['session'].strip()):
        raise ValueError('Read requires an explicitly selected conversation session')
    return result
