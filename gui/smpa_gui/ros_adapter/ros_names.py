"""Small naming rules shared by the discovery modules; no ROS imports."""

INFRASTRUCTURE_TOPICS = ('/rosout', '/parameter_events')
ACTION_MARKER = '/_action/'


def full_node_name(namespace, name):
    """Join a node's namespace and name into its fully qualified name.

    The root namespace is a single slash, so it must not produce a doubled
    slash. For example ``('/', 'talker')`` gives ``/talker`` and
    ``('/robot', 'talker')`` gives ``/robot/talker``.
    """
    return namespace.rstrip('/') + '/' + name


def is_hidden(name):
    """Tell whether a ROS name is hidden by convention.

    ROS treats a name as hidden when any of its slash-separated parts starts
    with an underscore. Command-line tools leave such names out unless asked.
    """
    return any(part.startswith('_') for part in name.split('/') if part)


def action_of(topic_or_service):
    """Return the action a hidden action topic or service belongs to, if any.

    An action named ``/fibonacci`` is implemented by entities such as
    ``/fibonacci/_action/status``. Return the action name for those, and
    ``None`` for ordinary names.
    """
    index = topic_or_service.find(ACTION_MARKER)
    return topic_or_service[:index] if index > 0 else None
