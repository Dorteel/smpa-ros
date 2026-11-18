"""Read SMPA's persisted stage records and world-model snapshots of one run, for the Dialogue's cognitive views.

Concepts:
    A stage record is one JSON file that an SMPA stage wrote for one
    invocation: ``state/cycle_<n>/<stage>[_<k>].json``. Sense, Model, Plan
    and Act records (``smpa_ros/sense.py``, ``model.py``, ``plan.py``,
    ``act.py``, ``act_node.py``) and the conversation's own records
    (``conversation.py``, ``belief.py``) share provenance fields:
    ``sequence``, ``invocation``, ``started_at``, ``prompt_name``,
    ``source_record`` and ``previous_record``. They are SMPA's authoritative
    history; this module only reads them.

    A link is a relationship the record itself states: its source record,
    its previous record, the evidence files it lists in
    ``source_observations``, the world-model snapshot it produced
    (``iteration``) or used (``previous_iteration``), and an observation
    file an Act produced (``observation_record``). Nothing is inferred: a
    link exists only when the record names it.

    A world-model snapshot is a Markdown file ``world_model/iteration_*.md``
    that the Model stage published exclusively, never replacing an earlier
    one, so the snapshots are the run's recorded world-model history.

    Values are shortened for transport (long texts are cut and say so); the
    complete record is always available through the session file route.
"""
import hashlib
import json
from pathlib import Path

STAGES = ('sense', 'model', 'plan', 'act')
TEXT_LIMIT = 20000
RECORD_LIMIT = 500
SNAPSHOT_LIMIT = 200
FILE_KINDS = {'.png': 'image', '.jpg': 'image', '.wav': 'audio', '.md': 'text', '.txt': 'text', '.json': 'json'}


def bounded(value, state):
    """Return a copy of JSON data with every long string cut to TEXT_LIMIT characters.

    ``state['truncated']`` is set when anything was cut, so the page can say
    that the full record is available as a file.
    """
    if isinstance(value, str):
        if len(value) > TEXT_LIMIT:
            state['truncated'] = True
            return value[:TEXT_LIMIT] + '…'
        return value
    if isinstance(value, dict):
        return {key: bounded(item, state) for key, item in value.items()}
    if isinstance(value, list):
        return [bounded(item, state) for item in value]
    return value


def inside(run_dir, relative):
    """Return the resolved path of a run-relative name if it stays inside the run, else None.

    Absolute names, names with backslashes and names that leave the run
    after resolving links are refused.
    """
    if not isinstance(relative, str) or not relative or relative.startswith('/') or '\\' in relative:
        return None
    base = run_dir.resolve()
    target = (base / relative.split('#', 1)[0]).resolve()
    return target if target.is_relative_to(base) else None


def link(run_dir, relation, relative):
    """Describe one stated link: relation, path, kind (record, snapshot or file kind) and whether it exists.

    A path outside the run is kept with ``exists`` false and ``outside``
    true, so a reader sees what the record claims without the GUI opening it.
    """
    target = inside(run_dir, relative)
    name = relative.split('#', 1)[0] if isinstance(relative, str) else ''
    if name.startswith('state/') and name.endswith('.json'):
        kind = 'record'
    elif name.startswith('world_model/') and name.endswith('.md'):
        kind = 'snapshot'
    else:
        kind = FILE_KINDS.get(Path(name).suffix.lower(), 'file')
    return dict(relation=relation, path=relative, kind=kind, exists=bool(target and target.is_file()),
                outside=target is None)


def record_links(run_dir, record):
    """List the links a record states, in a fixed order: provenance first, then evidence and snapshots."""
    links = []
    for relation, key in (('source', 'source_record'), ('previous', 'previous_record')):
        if isinstance(record.get(key), str) and record[key]:
            links.append(link(run_dir, relation, record[key]))
    for item in record.get('source_observations') or []:
        if isinstance(item, str):
            links.append(link(run_dir, 'evidence', item))
    for relation, key in (('produced_snapshot', 'iteration'), ('previous_snapshot', 'previous_iteration'),
                          ('observation', 'observation_record')):
        if isinstance(record.get(key), str) and record[key]:
            links.append(link(run_dir, relation, record[key]))
    return links


def kind_of(stage, record):
    """Name what kind of invocation a record describes: a cognitive stage, a conversation step or a belief update."""
    prompt = record.get('prompt_name')
    if stage == 'model' and prompt == 'conversation-belief':
        return 'belief_update'
    if prompt == 'conversation':
        return 'conversation'
    return 'stage'


def summary(stage, record):
    """Pick the fields of a record that its stage's view shows first; everything else stays in ``record``.

    Sense: the observation. Model: the snapshot produced and used, the model
    identifier and the world-model text (or, for a belief update, how many
    outputs were proposed). Plan: the selected action with its parameters
    and the actions that were available. Act: the requested action, its
    parameters, whether the goal was accepted, the result status, success,
    the result, the feedback received and a reason.
    """
    pick = lambda *keys: {key: record.get(key) for key in keys if key in record}
    if stage == 'sense':
        return pick('output', 'read_cursor')
    if stage == 'model':
        values = pick('iteration', 'previous_iteration', 'model_identifier', 'output', 'agent', 'ontology_sha256')
        if isinstance(record.get('outputs'), list):
            values['proposals'] = len(record['outputs'])
        return values
    if stage == 'plan':
        values = pick('selected_action', 'parameters', 'output', 'model_identifier')
        if isinstance(record.get('available_actions'), dict):
            values['available_actions'] = sorted(record['available_actions'])
        return values
    if stage == 'act':
        return pick('requested_action', 'parameters', 'goal', 'goal_accepted', 'result_status', 'success', 'result',
                    'feedback', 'reason', 'output', 'observation_record')
    return {}


def read_records(run_dir, stage=None):
    """Read the stage records of a run, optionally of one stage, in the order SMPA invoked them.

    Every ``state/cycle_*/*.json`` file is read; hidden files (SMPA's
    temporary and reservation files) are skipped. A record names its stage
    in ``stage``; older records without it are named by their file. A file
    that is not valid JSON is listed as ``unreadable`` instead of hiding the
    others. Records are ordered by ``sequence`` (then cycle and name). At most
    RECORD_LIMIT of the latest records are returned. Returns the records and
    a revision that changes whenever a record file is added or changed.
    """
    run_dir = Path(run_dir)
    found, fingerprint = [], hashlib.sha1()
    for path in sorted((run_dir / 'state').glob('cycle_*/*.json')):
        if path.name.startswith('.'):
            continue
        relative = str(path.relative_to(run_dir))
        status = path.stat()
        fingerprint.update(f'{relative}:{status.st_mtime_ns}:{status.st_size}'.encode())
        cycle = int(path.parent.name.split('_')[1]) if path.parent.name.split('_')[1].isdigit() else None
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(record, dict):
                raise ValueError('not an object')
        except (OSError, ValueError) as error:
            found.append(dict(path=relative, cycle=cycle, stage=path.stem.split('_')[0], status='unreadable',
                              error=str(error), sequence=None, links=[], summary={}, record={}, kind='stage', truncated=False))
            continue
        record_stage = record.get('stage') if isinstance(record.get('stage'), str) else path.stem.split('_')[0]
        if stage is not None and record_stage != stage:
            continue
        state = {}
        found.append(dict(
            path=relative, cycle=record.get('cycle', cycle), stage=record_stage, kind=kind_of(record_stage, record),
            status=record.get('status'), sequence=record.get('sequence'), invocation=record.get('invocation'),
            started_at=record.get('started_at'), prompt_name=record.get('prompt_name'), error=record.get('error'),
            links=record_links(run_dir, record), summary=bounded(summary(record_stage, record), state),
            record=bounded(record, state), truncated=bool(state.get('truncated'))))
    found.sort(key=lambda item: (item['sequence'] if isinstance(item['sequence'], int) else 10 ** 9,
                                 item['cycle'] or 0, item['path']))
    return found[-RECORD_LIMIT:], fingerprint.hexdigest()[:16]


def snapshots(run_dir):
    """List the run's world-model snapshots in order, each with its text and the record that produced it.

    The producing record is the Model record whose ``iteration`` names the
    snapshot; a snapshot no record claims is listed without one. Texts are
    shortened like records.
    """
    run_dir = Path(run_dir)
    producers = {}
    records, _ = read_records(run_dir, 'model')
    for entry in records:
        produced = entry['record'].get('iteration')
        if isinstance(produced, str):
            producers[produced] = entry['path']
    found = []
    for path in sorted((run_dir / 'world_model').glob('iteration_*.md'))[-SNAPSHOT_LIMIT:]:
        relative = str(path.relative_to(run_dir))
        state = {}
        text = bounded(path.read_text(encoding='utf-8', errors='replace'), state)
        found.append(dict(path=relative, size=path.stat().st_size, modified_at=path.stat().st_mtime,
                          produced_by=producers.get(relative), text=text, truncated=bool(state.get('truncated'))))
    return found


def latest_snapshot(run_dir):
    """Return the snapshot the latest successful Model invocation produced, or an empty string.

    This is what SMPA's experiment passes as ``previous_snapshot`` to the next
    Model call; the Dialogue uses the same rule when it calls Model.
    """
    records, _ = read_records(run_dir, 'model')
    for entry in reversed(records):
        produced = entry['record'].get('iteration')
        if entry['status'] == 'generated' and entry['kind'] == 'stage' and isinstance(produced, str):
            return produced
    return ''


def failure_after(run_dir, stage, sequence):
    """Return the error of the first failed record of a stage after a sequence number, or None.

    SMPA's stage nodes abort a goal without a message but persist the reason
    in their record; this finds that reason for the Dialogue to show.
    """
    records, _ = read_records(run_dir, stage)
    for entry in records:
        if isinstance(entry['sequence'], int) and entry['sequence'] > sequence and entry['status'] in ('failed', 'interrupted'):
            return entry['error'] or entry['status']
    return None


def last_sequence(run_dir):
    """Return the highest record sequence in the run, or 0 when it has none."""
    records, _ = read_records(run_dir)
    return max((entry['sequence'] for entry in records if isinstance(entry['sequence'], int)), default=0)
