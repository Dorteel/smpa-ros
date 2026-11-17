"""Read SMPA's recorded sessions from its run directories, strictly without changing them.

Concepts:
    The memory root is SMPA's ``memory/`` directory. Every session is one run
    directory under ``memory/runs/``, created by SMPA's MemoryManager. A robot
    run records Sense, Model, Plan and Act; a conversation run (``kind:
    conversation`` in its metadata) records conversational exchanges. The
    pre-run ``memory/legacy`` folder is shown as one archive session.

    An observation is one piece of sensory evidence SMPA recorded: a scene
    description, a camera frame, a focused visual answer, an audio transcript
    or a user message. A Sense record is the stage record SMPA wrote for one
    Sense invocation. This module turns both into one normalized, documented
    shape (see ``gui/SMPA_CONTRACTS.md``) without inventing data: fields SMPA
    did not record are absent or null, and every timestamp states its source.

    Activity combines what the metadata says with what the ROS graph shows.
    A robot run that is running owns ROS names under ``/run_<uuid>``; if none
    are visible, the run may have stopped without finalizing its metadata.
    Conversation runs have no ROS presence, so their liveness is unknown.

    This module opens files for reading only, imports nothing from SMPA and
    never takes locks, so it cannot interfere with a running session.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

import yaml

MEDIA_TYPES = {'.png': 'image/png', '.wav': 'audio/wav', '.md': 'text/markdown; charset=utf-8',
               '.json': 'application/json', '.txt': 'text/plain; charset=utf-8'}
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
VISION_NAME = re.compile(r'^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})_(\d{6})(Z?)_sense_output$')
TERMINAL = ('completed', 'interrupted', 'failed')
TEXT_LIMIT = 20000
OBSERVATION_LIMIT = 2000
LEGACY_ID = 'legacy-archive'
DEMO_MARKER = '.smpa-gui-demo'


def default_root(gui_directory):
    """Return SMPA's memory directory beside the GUI checkout, or None when there is none.

    The GUI normally lives in ``smpa-ros/gui``, so the memory root is
    ``smpa-ros/memory``. It counts only if it contains a ``runs`` directory.
    """
    candidate = Path(gui_directory).resolve().parent / 'memory'
    return candidate if (candidate / 'runs').is_dir() else None


def read_yaml(path):
    """Read a YAML file; return (data, None) or (None, explanation) without raising."""
    try:
        return yaml.safe_load(path.read_text(encoding='utf-8')), None
    except FileNotFoundError:
        return None, 'missing'
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        return None, f'unreadable: {type(error).__name__}'


def read_json(path):
    """Read a JSON file; return (data, None) or (None, explanation) without raising."""
    try:
        return json.loads(path.read_text(encoding='utf-8')), None
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return None, f'unreadable: {type(error).__name__}'


def parse_time(value):
    """Parse an ISO 8601 time stamp; return an aware datetime or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def file_time(path):
    """Return a file's modification time as an aware local datetime."""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone()


def stamp(moment, source):
    """Describe a time for the contract: ISO text, seconds since the epoch, and where it came from."""
    if moment is None:
        return dict(timestamp=None, t=None, timestamp_source=source)
    return dict(timestamp=moment.isoformat(), t=moment.timestamp(), timestamp_source=source)


def clip(text):
    """Shorten very long recorded text for display and say whether it was shortened."""
    if not isinstance(text, str):
        return text, False
    return (text[:TEXT_LIMIT] + '…', True) if len(text) > TEXT_LIMIT else (text, False)


def session_directories(root):
    """List (session id, directory) pairs for every run directory and the legacy archive.

    The session id is the run's UUID from its metadata, or the UUID in its
    directory name, or the directory name itself when neither exists.
    """
    found = []
    runs = Path(root) / 'runs'
    if runs.is_dir():
        for directory in sorted(runs.iterdir()):
            if not directory.is_dir() or directory.name.startswith('.'):
                continue
            metadata, _ = read_yaml(directory / 'metadata.yaml')
            identity = metadata.get('run_id') if isinstance(metadata, dict) else None
            if not isinstance(identity, str) or not identity:
                match = UUID.search(directory.name)
                identity = match.group(0) if match else directory.name
            found.append((identity, directory))
    legacy = Path(root) / 'legacy'
    if legacy.is_dir() and any(legacy.iterdir()):
        found.append((LEGACY_ID, legacy))
    return found


def modalities(configuration):
    """Work out which Sense modalities a robot run used, following SMPA's own rules.

    SMPA's legacy ``service`` setting (vision, speech, all) gives the defaults;
    explicit ``sensing.hear`` and ``sensing.vision`` booleans override them;
    Read is enabled only by ``sensing.read.enabled``. Malformed settings give
    ``None`` for the affected modality instead of a guess.
    """
    configuration = configuration if isinstance(configuration, dict) else {}
    pipeline = configuration.get('pipeline', configuration)
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    service = configuration.get('service', 'vision')
    result = dict(vision=service in ('vision', 'all'), hear=service in ('speech', 'all'), read=False)
    sensing = pipeline.get('sensing', {})
    if not isinstance(sensing, dict):
        return dict(vision=None, hear=None, read=None)
    for name in ('vision', 'hear'):
        if name in sensing:
            result[name] = sensing[name] if isinstance(sensing[name], bool) else None
    text = sensing.get('read', {})
    result['read'] = text.get('enabled', False) if isinstance(text, dict) else None
    return result


def ros_presence(identity, names):
    """Find the ROS names that belong to a run, as given by SMPA's run-scoped namespace.

    SMPA places a robot run's endpoints under ``/run_<uuid with underscores>``.
    ``names`` holds every node, topic, service and action name currently
    discovered, or is None when the ROS graph is unavailable.
    """
    if names is None:
        return dict(visible=None, names=[], nodes=[], listen_status_topics=[])
    prefix = '/run_' + identity.replace('-', '_')
    own = sorted(name for name in names['all'] if name == prefix or name.startswith(prefix + '/'))
    return dict(visible=bool(own), names=own[:200], nodes=[name for name in names['nodes'] if name in own],
                listen_status_topics=[name for name in own if name.endswith('/listen/_action/status')
                                      and name in names['topics']])


def describe_session(identity, directory, root, names, directory_index):
    """Summarise one session: identity, kind, lifecycle, modalities, binding and activity.

    Everything comes from the run's ``metadata.yaml`` and the ROS graph. A
    missing or unreadable metadata file gives kind ``unknown`` and an
    explanation instead of an error, so one damaged run never hides the others.
    """
    entry = dict(session_id=identity, directory=directory.name,
                 path=str(directory.relative_to(root)) if directory.is_relative_to(root) else str(directory),
                 name=directory.name, kind='unknown', status=None, started_at=None, completed_at=None,
                 completed_cycles=None, modalities=dict(vision=None, hear=None, read=None), camera_topic=None,
                 read_session=None, metadata_error=None)
    if identity == LEGACY_ID:
        entry.update(kind='legacy_archive', name='memory/legacy (archive from before run directories)',
                     modalities=dict(vision=True, hear=True, read=False))
    else:
        metadata, error = read_yaml(directory / 'metadata.yaml')
        if not isinstance(metadata, dict):
            entry['metadata_error'] = error or 'metadata is not a mapping'
        else:
            configuration = metadata.get('configuration') if isinstance(metadata.get('configuration'), dict) else {}
            pipeline = configuration.get('pipeline', {}) if isinstance(configuration.get('pipeline'), dict) else {}
            conversation = configuration.get('kind') == 'conversation'
            entry.update(kind='conversation' if conversation else 'robot', name=metadata.get('run_name') or directory.name,
                         status=metadata.get('status'), started_at=metadata.get('started_at'),
                         completed_at=metadata.get('completed_at'), completed_cycles=metadata.get('completed_cycles'))
            if conversation:
                entry['modalities'] = dict(vision=False, hear=False, read=True)
            else:
                entry['modalities'] = modalities(configuration)
                entry['camera_topic'] = pipeline.get('camera_topic') if isinstance(pipeline.get('camera_topic'), str) else None
                read = pipeline.get('sensing', {}).get('read', {}) if isinstance(pipeline.get('sensing'), dict) else {}
                if isinstance(read, dict) and isinstance(read.get('session'), str):
                    bound = Path(read['session']).name
                    entry['read_session'] = dict(path=read['session'], session_id=directory_index.get(bound))
    entry['ros'] = ros_presence(identity, names) if entry['kind'] == 'robot' else dict(
        visible=None, names=[], nodes=[], listen_status_topics=[])
    entry['activity'] = activity(entry)
    return entry


def activity(entry):
    """Classify a session as active, ended, unverified, not visible, or unknown.

    ``active``: a running robot run whose ROS names are visible.
    ``running_not_visible``: metadata says running but no ROS names are
    visible (the process may have stopped without finalizing, or runs in
    another ROS domain). ``running_unverified``: running, but liveness cannot
    be checked (conversation runs, or no ROS graph). ``ended``: a terminal
    status. ``unknown``: no readable status.
    """
    if entry['kind'] == 'legacy_archive':
        return 'ended'
    if entry['status'] in TERMINAL:
        return 'ended'
    if entry['status'] != 'running':
        return 'unknown'
    if entry['kind'] == 'robot' and entry['ros']['visible'] is not None:
        return 'active' if entry['ros']['visible'] else 'running_not_visible'
    return 'running_unverified'


def list_sessions(root, names=None):
    """Describe every session under the memory root, newest first.

    ``names`` comes from the ROS graph (or is None). Sessions without a start
    time sort last. Read bindings are resolved to session ids by directory name.
    """
    directories = session_directories(root)
    index = {directory.name: identity for identity, directory in directories}
    sessions = [describe_session(identity, directory, Path(root), names, index) for identity, directory in directories]
    sessions.sort(key=lambda item: (parse_time(item['started_at']) or datetime.min.replace(tzinfo=timezone.utc)),
                  reverse=True)
    return sessions


def find_session(root, identity):
    """Return the directory of a session id, or None when no such session exists."""
    for candidate, directory in session_directories(root):
        if candidate == identity:
            return directory
    return None


def run_offset(directory):
    """Return the UTC offset at which a run started, used for older local-time file names."""
    metadata, _ = read_yaml(directory / 'metadata.yaml')
    started = parse_time(metadata.get('started_at')) if isinstance(metadata, dict) else None
    return started.utcoffset() if started else None


def vision_time(stem, offset):
    """Read the time from a vision observation's file name.

    Current SMPA writes UTC names ending in ``Z``. Older names are local time
    without an offset; the run's own start offset is applied to them, or the
    gateway's local zone when that is unknown, and the source says so.
    """
    match = VISION_NAME.match(stem)
    if not match:
        return None, None
    day, hour, minute, second, micro, utc = match.groups()
    naive = datetime.fromisoformat(f'{day}T{hour}:{minute}:{second}.{micro}')
    if utc:
        return naive.replace(tzinfo=timezone.utc), 'file name (UTC)'
    if offset is not None:
        return naive.replace(tzinfo=timezone(offset)), "file name (local time; run's start offset applied)"
    return naive.astimezone(), 'file name (local time; offset assumed from the GUI computer)'


def png_camera(path):
    """Read the ``smpa.camera`` metadata SMPA stores inside a camera PNG, if present."""
    try:
        from PIL import Image
        with Image.open(path) as image:
            text = image.text.get('smpa.camera') if hasattr(image, 'text') else None
            size = image.size
    except Exception:
        return None, None
    try:
        return (json.loads(text) if text else None), size
    except ValueError:
        return None, size


def wav_seconds(path):
    """Return the duration of a WAV file from its header, or None when it cannot be read."""
    try:
        with path.open('rb') as handle:
            header = handle.read(4096)
    except OSError:
        return None
    if header[:4] != b'RIFF' or header[8:12] != b'WAVE':
        return None
    position, rate, size = 12, None, None
    while position + 8 <= len(header):
        name, length = header[position:position + 4], int.from_bytes(header[position + 4:position + 8], 'little')
        if name == b'fmt ' and length >= 12:
            rate = int.from_bytes(header[position + 16:position + 20], 'little')
        if name == b'data':
            size = length
            break
        position += 8 + length + (length % 2)
    return round(size / rate, 3) if rate and size is not None else None


def transcript_fields(text):
    """Split SMPA's audio ``transcript.md`` into its ``- Key: value`` fields and transcript body."""
    fields, body = {}, ''
    head, _, rest = text.partition('## Transcript')
    for line in head.splitlines():
        if line.startswith('- ') and ': ' in line:
            key, value = line[2:].split(': ', 1)
            fields[key.strip()] = value.strip()
    body = rest.strip()
    return fields, body


def file_url(identity, relative):
    """Build the gateway address of one recorded file of a session."""
    from urllib.parse import quote
    return f'/api/v1/smpa/sessions/{quote(identity)}/files?path={quote(relative)}'


def observation(identity, kind, modality, moment, time_source, **fields):
    """Assemble one normalized observation with the fields every observation has.

    ``moment`` is when the evidence was recorded and ``time_source`` says
    where that time was read from; ``fields`` fill in the rest.
    """
    base = dict(session_id=identity, modality=modality, kind=kind, recorded=True, status=None, text=None,
                text_truncated=False, image=None, audio=None, source=None, sense_record=None, cycle=None,
                epistemic_status=None, provenance={})
    base.update(stamp(moment, time_source))
    base.update(fields)
    base['id'] = hashlib.sha1(f"{identity}|{kind}|{base['source']}|{base.get('item', '')}".encode()).hexdigest()[:16]
    base.pop('item', None)
    return base


def vision_observations(identity, directory, linked):
    """Collect recorded visual evidence: descriptions, camera frames and focused answers."""
    results, offset = [], run_offset(directory)
    for folder in (directory / 'observations' / 'vision', directory / 'vision', directory):
        if not folder.is_dir() or (folder == directory and directory.name != 'legacy'):
            continue
        for path in sorted(folder.iterdir()):
            relative = str(path.relative_to(directory))
            if path.suffix == '.md' and path.stem.endswith('_sense_output'):
                moment, source = vision_time(path.stem, offset)
                text, cut = clip(path.read_text(encoding='utf-8', errors='replace'))
                image = path.with_suffix('.png')
                camera, size = png_camera(image) if image.is_file() else (None, None)
                extra = path.with_suffix('.json')
                details, _ = read_json(extra) if extra.is_file() else (None, None)
                results.append(observation(
                    identity, 'scene_description', 'vision', moment or file_time(path), source or 'file modification time',
                    status='generated', text=text, text_truncated=cut, source=relative,
                    image=file_url(identity, str(image.relative_to(directory))) if image.is_file() else None,
                    sense_record=linked.get(relative), provenance=dict(camera=camera, image_size=size, details=details)))
            elif path.suffix == '.png' and path.stem.endswith('_sense_output') and not path.with_suffix('.md').is_file():
                moment, source = vision_time(path.stem, offset)
                camera, size = png_camera(path)
                received = parse_time((camera or {}).get('received_at'))
                results.append(observation(
                    identity, 'camera_frame', 'vision', received or moment or file_time(path),
                    'PNG smpa.camera received_at' if received else source or 'file modification time',
                    status='evidence_without_description', source=relative, image=file_url(identity, relative),
                    sense_record=linked.get(relative), provenance=dict(camera=camera, image_size=size)))
            elif path.name == 'frozen_image.png':
                camera, size = png_camera(path)
                results.append(observation(
                    identity, 'camera_frame', 'vision', file_time(path), 'file modification time',
                    status='frozen_for_refinement', source=relative, image=file_url(identity, relative),
                    sense_record=linked.get(relative), provenance=dict(camera=camera, image_size=size)))
            elif path.suffix == '.json' and path.name.startswith('observe_'):
                record, error = read_json(path)
                if not isinstance(record, dict):
                    results.append(observation(identity, 'focused_observation', 'vision', file_time(path),
                                               'file modification time', status=error, source=relative))
                    continue
                answer, status = record.get('output'), 'recorded'
                try:
                    parsed = json.loads(answer) if isinstance(answer, str) else None
                    if isinstance(parsed, dict) and 'answer' in parsed:
                        answer, status = parsed['answer'], parsed.get('status', status)
                except ValueError:
                    pass
                text, cut = clip(answer if isinstance(answer, str) else json.dumps(answer))
                results.append(observation(
                    identity, 'focused_observation', 'vision', file_time(path), 'file modification time',
                    status=status, text=text, text_truncated=cut, source=relative, sense_record=linked.get(relative),
                    provenance={key: record.get(key) for key in ('target', 'question', 'image_sha256', 'model_identifier')}))
    return results


def audio_observations(identity, directory, linked):
    """Collect recorded auditory evidence: each recording with its transcript."""
    results = []
    for folder in (directory / 'observations' / 'audio', directory / 'audio'):
        if not folder.is_dir():
            continue
        for session in sorted(item for item in folder.iterdir() if item.is_dir()):
            transcript = session / 'transcript.md'
            recording = session / 'recording.wav'
            relative = str(transcript.relative_to(directory)) if transcript.is_file() else str(session.relative_to(directory))
            fields, body = ({}, None)
            if transcript.is_file():
                fields, body = transcript_fields(transcript.read_text(encoding='utf-8', errors='replace'))
            moment = parse_time(fields.get('Timestamp'))
            text, cut = clip(body)
            link = linked.get(relative) or linked.get(str(recording.relative_to(directory)))
            results.append(observation(
                identity, 'audio_transcript', 'hear', moment or file_time(transcript if transcript.is_file() else session),
                'transcript Timestamp' if moment else 'file modification time',
                status=(fields.get('Status') or ('transcript missing' if not transcript.is_file() else None)),
                text=text, text_truncated=cut, source=relative, sense_record=link,
                audio=file_url(identity, str(recording.relative_to(directory))) if recording.is_file() else None,
                provenance=dict(fields, duration_s=wav_seconds(recording) if recording.is_file() else None)))
    return results


def sense_records(identity, directory, kind):
    """Read every Sense stage record, newest cycle last.

    Return (stage entries, read observations, file-to-record links). For a
    conversation run each Sense record holds one user message. For a robot
    run, a Read result inside the record contributes its user messages, and
    the record's ``source_observations`` (and, in scene refinement, the
    ``source_record`` of each focused observation it incorporated) link
    evidence files to it.
    """
    stages, reads, linked = [], [], {}
    state = directory / 'state'
    if not state.is_dir():
        return stages, reads, linked
    for path in sorted(state.glob('cycle_*/sense*.json')):
        relative = str(path.relative_to(directory))
        record, error = read_json(path)
        if not isinstance(record, dict):
            stages.append(observation(identity, 'sense_record', 'sense', file_time(path), 'file modification time',
                                      status=error, source=relative))
            continue
        moment = parse_time(record.get('started_at'))
        source = 'Sense record started_at' if moment else 'file modification time'
        moment = moment or file_time(path)
        provenance = {key: record.get(key) for key in ('sequence', 'invocation', 'prompt_name', 'previous_record',
                                                        'source_record', 'error') if key in record}
        for evidence in record.get('source_observations') or []:
            if isinstance(evidence, str):
                linked[evidence] = relative
        if kind == 'conversation':
            text, cut = clip(record.get('output') if isinstance(record.get('output'), str) else None)
            reads.append(observation(identity, 'user_message', 'read', moment, source, status=record.get('status'),
                                     text=text, text_truncated=cut, source=relative, sense_record=relative,
                                     cycle=record.get('cycle'), epistemic_status='unverified_user_statement',
                                     provenance=dict(provenance, role='user')))
            continue
        output = record.get('output')
        try:
            parsed = json.loads(output) if isinstance(output, str) else None
        except ValueError:
            parsed = None
        modalities_seen = sorted(parsed) if isinstance(parsed, dict) else []
        for focused in (parsed.get('focused_observations') or []) if isinstance(parsed, dict) else []:
            if isinstance(focused, dict) and isinstance(focused.get('source_record'), str):
                linked.setdefault(focused['source_record'], relative)
        stages.append(observation(
            identity, 'sense_record', 'sense', moment, source, status=record.get('status'), source=relative,
            sense_record=relative, cycle=record.get('cycle'),
            text=record.get('error') or (f"Sense output contains: {', '.join(modalities_seen)}" if modalities_seen else None),
            provenance=dict(provenance, source_observations=record.get('source_observations'),
                            read_cursor=record.get('read_cursor'))))
        read = parsed.get('read') if isinstance(parsed, dict) else None
        if isinstance(read, dict):
            for index, item in enumerate(read.get('observations') or []):
                if not isinstance(item, dict):
                    continue
                when = parse_time(item.get('timestamp'))
                text, cut = clip(item.get('content'))
                reads.append(observation(
                    identity, 'user_message', 'read', when or moment,
                    'conversation Sense record started_at' if when else source, status=record.get('status'),
                    text=text, text_truncated=cut, source=item.get('source') or relative, sense_record=relative,
                    cycle=record.get('cycle'), epistemic_status=item.get('epistemic_status'), item=index,
                    provenance=dict(role=item.get('role'), message_index=item.get('message_index'),
                                    conversation_session_id=read.get('session_id'),
                                    conversation_source_record=item.get('source_record'))))
            if read.get('deferred'):
                stages[-1]['provenance']['read_deferred'] = read['deferred']
    return stages, reads, linked


def revision(directory):
    """Fingerprint a session directory from file names, sizes and modification times.

    Clients compare it between polls to skip work when nothing changed.
    """
    digest = hashlib.sha1()
    for path in sorted(directory.rglob('*')):
        if path.is_file() and not path.name.startswith('.'):
            info = path.stat()
            digest.update(f'{path.relative_to(directory)}|{info.st_size}|{info.st_mtime_ns}\n'.encode())
    return digest.hexdigest()[:16]


def session_observations(root, identity, modality=None):
    """Return every recorded observation of a session in chronological order.

    Sense records are included as entries of modality ``sense``, so failed
    invocations remain visible. ``modality`` optionally keeps only vision,
    hear, read or sense entries. At most ``OBSERVATION_LIMIT`` of the newest
    entries are returned; ``truncated`` says when older ones were left out.
    Returns None when the session does not exist.
    """
    directory = find_session(root, identity)
    if directory is None:
        return None
    metadata, _ = read_yaml(directory / 'metadata.yaml')
    kind = 'conversation' if isinstance(metadata, dict) and isinstance(metadata.get('configuration'), dict) \
        and metadata['configuration'].get('kind') == 'conversation' else 'robot'
    warnings = []
    try:
        stages, reads, linked = sense_records(identity, directory, kind)
        entries = stages + reads + vision_observations(identity, directory, linked) + \
            audio_observations(identity, directory, linked)
    except OSError as error:
        entries, warnings = [], [f'The session could not be read completely: {error}']
    if modality:
        entries = [entry for entry in entries if entry['modality'] == modality]
    entries.sort(key=lambda entry: (entry['t'] if entry['t'] is not None else float('-inf'), entry['source'] or ''))
    truncated = len(entries) > OBSERVATION_LIMIT
    return dict(session_id=identity, revision=revision(directory), observations=entries[-OBSERVATION_LIMIT:],
                truncated=truncated, warnings=warnings)


def recorded_file(root, identity, relative):
    """Resolve a recorded file of a session for download, or return None when it is not allowed.

    The path must be relative, stay inside the session directory after
    resolving links, name an existing file, and have one of the recorded
    media types. Nothing outside SMPA's session directories can be read.
    """
    directory = find_session(root, identity)
    if directory is None or not relative or relative.startswith('/') or '\\' in relative:
        return None
    base = directory.resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base) or not target.is_file() or target.suffix not in MEDIA_TYPES:
        return None
    return target, MEDIA_TYPES[target.suffix]


def graph_names(graph):
    """Collect node, topic, service and action names from a graph description for presence checks."""
    nodes = {node['full_name'] for node in graph['nodes']}
    topics = {topic['name'] for topic in graph['topics']}
    every = nodes | topics | {service['name'] for service in graph['services']} | {action['name'] for action in graph['actions']}
    return dict(all=every, nodes=nodes, topics=topics)
