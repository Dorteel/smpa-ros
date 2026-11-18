"""Where the GUI finds SMPA: its checkout, interpreter, configuration, memory and per-run files.

Concepts:
    The layout is the GUI's configuration of paths. Nothing in it is an
    architectural fact about SMPA: every entry has a default that matches
    the repository as it is today, and every entry can be overridden by a
    YAML layout file or an environment variable.

    SMPA checkout layouts: since the ROS 2 package restructuring, SMPA's
    conversation server is ``<root>/smpa_ros/smpa_ros/conversation_web.py``
    with ``memory.py`` beside the package in ``<root>/smpa_ros`` and the
    shared inference code in ``<root>/smpa_llm`` ("packages" layout). Before
    it, everything was in ``<root>`` ("flat" layout). Both are recognised.

    Per-run files are given relative to a run directory and must stay
    inside it. Each one is a list of candidate paths; the first that exists
    is used, so the GUI can follow a future location while still showing
    where SMPA writes today. The requested target structure is
    ``world_model/{conversation.md,sense.ttl,belief.ttl,plan.ttl}``; today
    SMPA writes ``conversation.md`` and ``belief-state.ttl`` at the run root.

    Defaults (overridable):
        smpa_root          the repository that contains this source checkout of smpa_gui,
                           when it holds an SMPA conversation server; otherwise not configured
        config             ``<root>/smpa_ros/config.yaml`` (packages) or ``<root>/config.yaml`` (flat)
        python             ``<root>/.venv/bin/python`` (SMPA's own environment)
        model_root         ``<root>/models``
        memory_root        ``<root>/memory``
        conversation_port  8766 (port of the GUI-managed SMPA conversation server)
        conversation_url   none (set it to use an already running conversation server)
        mock_graphs        the ``mock_graphs`` folder installed with the GUI
        files.conversation [world_model/conversation.md, conversation.md]
        files.sense        [world_model/sense.ttl]
        files.belief       [world_model/belief.ttl, belief-state.ttl]
        files.plan         [world_model/plan.ttl]

    Relative paths in a layout file are relative to the file's folder.
    Environment variables: ``SMPA_GUI_LAYOUT`` (YAML file), ``SMPA_GUI_SMPA_ROOT``,
    ``SMPA_GUI_SMPA_CONFIG``, ``SMPA_GUI_SMPA_PYTHON``, ``SMPA_GUI_MEMORY_ROOT``,
    ``SMPA_GUI_CONVERSATION_PORT``, ``SMPA_GUI_CONVERSATION_URL``, ``SMPA_GUI_MOCK_GRAPHS``.
"""
import os
from pathlib import Path, PurePosixPath

import yaml

PACKAGE = Path(__file__).resolve().parent.parent
SOURCE_PACKAGE = PACKAGE.parent
GRAPH_VIEWS = ('sense', 'belief', 'plan')
DEFAULT_FILES = {'conversation': ['world_model/conversation.md', 'conversation.md'],
                 'sense': ['world_model/sense.ttl'],
                 'belief': ['world_model/belief.ttl', 'belief-state.ttl'],
                 'plan': ['world_model/plan.ttl']}
KNOWN_KEYS = {'smpa_root', 'config', 'python', 'model_root', 'memory_root', 'conversation_port',
              'conversation_url', 'mock_graphs', 'files', 'ros', 'dialogue'}


class LayoutError(ValueError):
    """The layout configuration is invalid; the message names the entry."""


def check_relative(name, value):
    """Accept a per-run path only if it is relative and cannot leave the run directory."""
    if not isinstance(value, str) or not value.strip():
        raise LayoutError(f'files.{name} must contain non-empty relative paths')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\\' in value:
        raise LayoutError(f'files.{name} must stay inside the run directory: {value}')
    return str(path)


def checkout_layout(root):
    """Name the layout of an SMPA checkout: ``packages``, ``flat``, or ``None`` when it holds no conversation server."""
    if root is None:
        return None
    if (Path(root) / 'smpa_ros' / 'smpa_ros' / 'conversation_web.py').is_file():
        return 'packages'
    if (Path(root) / 'smpa_ros' / 'conversation_web.py').is_file():
        return 'flat'
    return None


def default_root():
    """Return the repository that contains this source checkout of the GUI, if it holds SMPA; otherwise None.

    A source checkout lives at ``<repository>/smpa_gui``. An installed
    package lives in an install space, whose parent is not an SMPA
    checkout, so there the root must be configured.
    """
    candidate = SOURCE_PACKAGE.parent
    return candidate if checkout_layout(candidate) else None


def python_path(root):
    """List the source folders that make SMPA's modules importable from a checkout, in import order."""
    if checkout_layout(root) == 'packages':
        return [Path(root) / 'smpa_ros', Path(root) / 'smpa_llm']
    return [Path(root)] if root else []


def load(path=None, environment=None):
    """Build the effective layout from defaults, an optional YAML file and environment variables.

    Later sources win: defaults, then the layout file, then individual
    environment variables. Paths are made absolute. Unknown keys in the file
    are refused so that a misspelt setting is not silently ignored. A
    per-run file may be given as one path or as a list of candidates. The
    ``ros`` section (ROS endpoint names of SMPA's existing interfaces) and
    the ``dialogue`` section (belief context and stage adapters) are kept
    as given after they have been checked.
    """
    environment = os.environ if environment is None else environment
    path = path or environment.get('SMPA_GUI_LAYOUT')
    supplied = {}
    if path:
        supplied = yaml.safe_load(Path(path).expanduser().read_text(encoding='utf-8')) or {}
        if not isinstance(supplied, dict):
            raise LayoutError('The layout file must contain a mapping')
        unknown = set(supplied) - KNOWN_KEYS
        if unknown:
            raise LayoutError(f'Unknown layout entries: {", ".join(sorted(unknown))}')
        folder = Path(path).expanduser().resolve().parent
        for key in ('smpa_root', 'config', 'python', 'model_root', 'memory_root', 'mock_graphs'):
            if supplied.get(key):
                supplied[key] = folder / Path(str(supplied[key])).expanduser()
    chosen = environment.get('SMPA_GUI_SMPA_ROOT') or supplied.get('smpa_root')
    root = Path(chosen).expanduser().resolve() if chosen else default_root()
    flat = checkout_layout(root) != 'packages'
    base = root or Path('/nonexistent-smpa-root')
    layout = dict(
        smpa_root=root, smpa_layout=checkout_layout(root),
        config=Path(environment.get('SMPA_GUI_SMPA_CONFIG') or supplied.get('config')
                    or (base / 'config.yaml' if flat else base / 'smpa_ros' / 'config.yaml')),
        python=Path(environment.get('SMPA_GUI_SMPA_PYTHON') or supplied.get('python') or base / '.venv/bin/python'),
        model_root=Path(supplied.get('model_root') or base / 'models'),
        memory_root=Path(environment.get('SMPA_GUI_MEMORY_ROOT') or supplied.get('memory_root') or base / 'memory'),
        conversation_port=int(environment.get('SMPA_GUI_CONVERSATION_PORT') or supplied.get('conversation_port') or 8766),
        conversation_url=(environment.get('SMPA_GUI_CONVERSATION_URL') or supplied.get('conversation_url') or None),
        mock_graphs=Path(environment.get('SMPA_GUI_MOCK_GRAPHS') or supplied.get('mock_graphs') or PACKAGE / 'mock_graphs'),
        files={name: list(paths) for name, paths in DEFAULT_FILES.items()}, source=str(path) if path else None)
    for key in ('config', 'python', 'model_root', 'memory_root', 'mock_graphs'):
        layout[key] = layout[key].expanduser().absolute()
    files = supplied.get('files') or {}
    if not isinstance(files, dict) or set(files) - set(DEFAULT_FILES):
        raise LayoutError(f'files accepts only: {", ".join(DEFAULT_FILES)}')
    for name, value in files.items():
        values = value if isinstance(value, list) else [value]
        if not values:
            raise LayoutError(f'files.{name} needs at least one path')
        layout['files'][name] = [check_relative(name, item) for item in values]
    layout['ros'] = supplied.get('ros') or {}
    layout['dialogue'] = supplied.get('dialogue') or {}
    from smpa_gui.smpa_adapter import dialogue, smpa_services
    try:
        smpa_services.endpoints(layout['ros'])
        dialogue.configuration(layout['dialogue'])
    except ValueError as error:
        raise LayoutError(str(error)) from error
    return layout


def describe(layout):
    """Return the layout as plain data for the API, without anything secret."""
    result = {key: (str(value) if isinstance(value, Path) else value) for key, value in layout.items()}
    result['python_present'] = layout['python'].is_file()
    result['config_present'] = layout['config'].is_file()
    result['conversation_server_present'] = layout['smpa_layout'] is not None
    return result


def run_file(run_directory, layout, name):
    """Resolve one per-run file inside a run directory and say which candidate was used.

    Try each configured candidate in order and return the first that
    exists, as ``(path, relative path, is_fallback)``; ``is_fallback`` is
    true when it is not the first (preferred) candidate. When none exists,
    the preferred candidate is returned, so the caller reports it as
    missing. A candidate that would leave the run directory (for example
    through a symbolic link) is refused.
    """
    base = Path(run_directory).resolve()
    candidates = layout['files'][name]
    resolved = []
    for relative in candidates:
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise LayoutError(f'files.{name} leaves the run directory: {relative}')
        resolved.append((target, relative))
    for index, (target, relative) in enumerate(resolved):
        if target.exists():
            return target, relative, index > 0
    return resolved[0][0], resolved[0][1], False
