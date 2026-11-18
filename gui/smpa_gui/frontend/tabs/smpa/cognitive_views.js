/**
 * Recorded history of SMPA's cognitive stages: record cards for Sense, Model, Plan and Act, and world-model snapshots.
 *
 * Concepts: a record is one JSON file an SMPA stage persisted for one
 * invocation (served by GET /smpa/sessions/{id}/records). A card shows the
 * fields that matter for its stage, the links the record itself states
 * (source record, previous record, evidence files, snapshots), and the
 * complete record on request. Nothing is inferred: a link is drawn only when
 * the record names it. Evidence images and audio are shown from the session's
 * read-only file route. Snapshots are the run's world-model history
 * (GET /smpa/sessions/{id}/world-model), newest first.
 *
 * Everything here is persisted history; live ROS status is drawn elsewhere
 * and labelled as live. The module only builds elements; the Dialogue
 * decides when to fetch.
 */
import {h} from '../../api.js';

export const STAGE_TITLES = {sense: 'Sense', model: 'Model', plan: 'Plan', act: 'Act'};
const KIND_TEXT = {belief_update: 'belief update (conversation)', conversation: 'conversation step', stage: 'cognitive stage'};
const RELATION_TEXT = {source: 'source record', previous: 'previous record', evidence: 'evidence',
  produced_snapshot: 'produced snapshot', previous_snapshot: 'previous snapshot', observation: 'observation'};

/** Return the URL of a run-relative file of a session, served read-only by the GUI. */
export function fileUrl(sessionId, path) {
  return `/api/v1/smpa/sessions/${encodeURIComponent(sessionId)}/files?path=${encodeURIComponent(path.split('#')[0])}`;
}

/** Return the stage a record path belongs to ("state/cycle_000001/model_002.json" → "model"), or null. */
export function stageOfPath(path) {
  const match = /^state\/cycle_\d+\/([a-z]+)(?:_\d+)?\.json$/.exec(path || '');
  return match ? match[1] : null;
}

/** Show a value compactly: text as it is, anything else as indented JSON. */
function valueBlock(value) {
  if (value === null || value === undefined) return h('span', {class: 'muted'}, '–');
  if (typeof value === 'string') return h('div', {class: 'record-text'}, value);
  return h('pre', {class: 'record-json'}, JSON.stringify(value, null, 2));
}

/** Parse a text that may hold a JSON object; return the object or null. */
function jsonObject(text) {
  if (typeof text !== 'string' || !text.trim().startsWith('{')) return null;
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch (error) {
    return null;
  }
}

/** Build a definition list from label/value pairs, skipping values that are absent. */
function facts(pairs) {
  const shown = pairs.filter(([, value]) => value !== undefined && value !== null && value !== '');
  return shown.length ? h('dl', {class: 'record-facts'}, shown.flatMap(([label, value]) => [h('dt', {}, label),
    h('dd', {}, value && value.nodeType ? value : valueBlock(value))])) : null;
}

/** Build a small status pill for a record's status. */
function statusPill(status) {
  const style = {generated: 'pill-ok', succeeded: 'pill-ok', completed: 'pill-ok', failed: 'pill-off', interrupted: 'pill-warn',
    unreadable: 'pill-off'}[status] || '';
  return h('span', {class: `pill ${style}`, 'data-status': status || ''}, status || 'no status');
}

/** Build the stage-specific body of a record card from its summary fields. */
function stageBody(record) {
  const summary = record.summary || {};
  if (record.stage === 'sense') {
    const observation = jsonObject(summary.output);
    return observation
      ? facts(Object.entries(observation).map(([key, value]) => [key, typeof value === 'string' ? value : value]))
      : facts([['observation', summary.output]]);
  }
  if (record.stage === 'model') {
    if (record.kind === 'belief_update') {
      return facts([['proposals', summary.proposals], ['agent', summary.agent && (summary.agent.source || summary.agent.model_id)],
        ['ontology', summary.ontology_sha256]]);
    }
    return facts([['model', summary.model_identifier],
      ['world model', summary.output ? h('details', {class: 'record-world'}, h('summary', {}, 'Show the world model text'),
        h('pre', {class: 'record-json'}, summary.output)) : null]]);
  }
  if (record.stage === 'plan') {
    return facts([['selected action', summary.selected_action ? h('strong', {class: 'record-action'}, summary.selected_action) : null],
      ['parameters', summary.parameters], ['available', summary.available_actions && summary.available_actions.join(', ')],
      ['model', summary.model_identifier], ['output', summary.selected_action ? null : summary.output]]);
  }
  if (record.stage === 'act') {
    const flag = (value) => (value === true ? h('span', {class: 'pill pill-ok'}, 'yes') : value === false ? h('span', {class: 'pill pill-off'}, 'no') : null);
    return facts([['requested action', summary.requested_action !== undefined
      ? h('strong', {class: 'record-action'}, summary.requested_action === null ? 'none' : summary.requested_action) : null],
    ['parameters', summary.parameters], ['goal accepted', flag(summary.goal_accepted)], ['result status', summary.result_status],
    ['success', flag(summary.success)], ['result', summary.result], ['feedback', summary.feedback && summary.feedback.length
      ? h('ol', {class: 'record-feedback'}, summary.feedback.map((item) => h('li', {}, typeof item === 'string' ? item : JSON.stringify(item))))
      : null], ['reason', summary.reason], ['reply', summary.output]]);
  }
  return null;
}

/**
 * Build the list of links a record states.
 *
 * A link to another record jumps to that record's card (``onOpen``); a
 * snapshot or file opens read-only in a new tab; an image is also shown as a
 * thumbnail and audio as a player. A link to something that does not exist
 * or lies outside the run is shown but not followed.
 */
function linkList(record, sessionId, onOpen) {
  if (!record.links || !record.links.length) return null;
  return h('ul', {class: 'record-links'}, record.links.map((link) => {
    const label = h('span', {class: 'muted'}, `${RELATION_TEXT[link.relation] || link.relation}: `);
    if (!link.exists) {
      return h('li', {'data-relation': link.relation}, label, h('span', {class: 'mono'}, link.path),
        h('span', {class: 'muted'}, link.outside ? ' (outside this run; not opened)' : ' (not found)'));
    }
    if (link.kind === 'record') {
      return h('li', {'data-relation': link.relation}, label,
        h('a', {href: '#', class: 'mono record-jump', 'data-target': link.path,
          onclick: (event) => { event.preventDefault(); onOpen(link.path); }}, link.path));
    }
    const url = fileUrl(sessionId, link.path);
    return h('li', {'data-relation': link.relation}, label, h('a', {href: url, target: '_blank', rel: 'noopener', class: 'mono'}, link.path),
      link.kind === 'image' ? h('img', {class: 'record-thumb', src: url, alt: link.path, loading: 'lazy'}) : null,
      link.kind === 'audio' ? h('audio', {controls: true, preload: 'none', src: url}) : null);
  }));
}

/** Build one record card: heading, stage body, error, links, and the complete record on request. */
export function recordCard(record, sessionId, onOpen) {
  return h('article', {class: `record-card record-${record.stage}`, 'data-path': record.path, 'data-stage': record.stage,
    'data-status': record.status || '', 'data-kind': record.kind},
  h('header', {class: 'record-head'},
    h('strong', {}, STAGE_TITLES[record.stage] || record.stage), statusPill(record.status),
    h('span', {class: 'muted'}, [record.cycle ? `cycle ${record.cycle}` : null, record.sequence ? `#${record.sequence}` : null,
      record.invocation > 1 ? `invocation ${record.invocation}` : null].filter(Boolean).join(' · ')),
    record.kind !== 'stage' ? h('span', {class: 'pill'}, KIND_TEXT[record.kind] || record.kind) : null,
    record.prompt_name ? h('span', {class: 'muted mono'}, `prompt ${record.prompt_name}`) : null,
    h('span', {class: 'muted', style: 'margin-left:auto'}, record.started_at ? new Date(record.started_at).toLocaleString() : '')),
  stageBody(record),
  record.error ? h('p', {class: 'error-text record-error'}, record.error) : null,
  linkList(record, sessionId, onOpen),
  h('details', {class: 'record-raw'}, h('summary', {}, 'Complete record'),
    h('pre', {class: 'record-json'}, JSON.stringify(record.record, null, 2)),
    h('a', {href: fileUrl(sessionId, record.path), target: '_blank', rel: 'noopener', class: 'mono'}, record.path),
    record.truncated ? h('span', {class: 'muted'}, ' (long texts are shortened here; the file is complete)') : null));
}

/** Build the record list of one stage, newest first, or explain that the run has none. */
export function recordList(stage, records, sessionId, onOpen) {
  const shown = records.filter((record) => record.stage === stage).slice().reverse();
  if (!shown.length) {
    return [h('p', {class: 'muted empty', id: 'records-empty'}, `This session has no ${STAGE_TITLES[stage]} records yet.`)];
  }
  return shown.map((record) => recordCard(record, sessionId, onOpen));
}

/** Build the world-model history: every snapshot newest first, with the record that produced it. */
export function snapshotList(snapshots, sessionId, onOpen) {
  if (!snapshots.length) {
    return [h('p', {class: 'muted empty', id: 'snapshots-empty'},
      'This session has no world-model snapshots (world_model/iteration_*.md). Conversations keep their beliefs in the belief graph instead.')];
  }
  return snapshots.slice().reverse().map((snapshot, index) => h('article', {class: 'record-card snapshot-card', 'data-path': snapshot.path},
    h('header', {class: 'record-head'}, h('strong', {}, index === 0 ? 'Latest snapshot' : 'Snapshot'),
      h('a', {href: fileUrl(sessionId, snapshot.path), target: '_blank', rel: 'noopener', class: 'mono'}, snapshot.path),
      h('span', {class: 'muted', style: 'margin-left:auto'}, new Date(snapshot.modified_at * 1000).toLocaleString())),
    snapshot.produced_by ? h('p', {class: 'muted', style: 'margin:2px 0'}, 'produced by ',
      h('a', {href: '#', class: 'mono record-jump', 'data-target': snapshot.produced_by,
        onclick: (event) => { event.preventDefault(); onOpen(snapshot.produced_by); }}, snapshot.produced_by))
      : h('p', {class: 'muted', style: 'margin:2px 0'}, 'No Model record names this snapshot.'),
    h('pre', {class: 'record-json snapshot-text'}, snapshot.text)));
}
