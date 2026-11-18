/**
 * The Sense section: See, Hear and Read panels and one chronological observation timeline.
 *
 * Concepts: a recorded observation is evidence SMPA wrote into the session's
 * run directory (scene descriptions, camera frames, focused answers, audio
 * transcripts, user messages) and Sense records (one per Sense invocation,
 * including failed ones). Live data is what ROS shows right now: the camera
 * topic and the state of a run's Listen action. Live data is never stored by
 * the GUI and is always labelled LIVE, so it cannot be mistaken for what SMPA
 * recorded.
 *
 * This section only reads. It offers no way to submit text or create
 * sessions; conversational input is entered on the Dialogue (SMPA's conversation server).
 */
import {fmt, getJSON, h, openStream, poll} from '../../api.js';
import * as imageViewer from '../sensors/viewers/image.js';

const MODALITY_LABEL = {vision: 'See', hear: 'Hear', read: 'Read', sense: 'Sense record'};
const KIND_LABEL = {scene_description: 'scene description', camera_frame: 'camera frame', focused_observation: 'focused observation',
                    audio_transcript: 'audio transcript', user_message: 'user message', sense_record: 'Sense invocation'};
const filters = {vision: true, hear: true, read: true, sense: true, newestFirst: true};
let session = null;
let observations = [];
let revision = null;
let truncated = false;
let loadError = null;
let dom = {};
let stops = [];
let live = {camera: null, cameraTopic: null, cameraClose: null, listenClose: null, listenTopic: null};
let expanded = new Set();

/** Format a recorded time stamp as local date and time with milliseconds. */
function when(observation) {
  if (!observation.timestamp) return 'time unknown';
  const date = new Date(observation.t * 1000);
  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour12: false}) + '.' +
    String(date.getMilliseconds()).padStart(3, '0');
}

/** Shorten a source path for display: paths inside a memory directory start at "runs/". */
function shortSource(source) {
  if (!source) return '';
  const index = source.indexOf('/runs/');
  return index >= 0 ? source.slice(index + 1) : source;
}

/** Build the small "recorded by SMPA" label with time, time source and source file. */
function recordedLine(observation) {
  return h('div', {class: 'muted', style: 'font-size:12px'},
    h('span', {class: 'pill', style: 'font-size:11px'}, 'RECORDED'), ' ', when(observation),
    h('span', {title: `Time taken from: ${observation.timestamp_source}`}, ' ⓘ'),
    observation.status ? ` · ${observation.status}` : '',
    ' · ', h('span', {class: 'mono', style: 'word-break:break-all', title: observation.source || ''}, shortSource(observation.source)));
}

/** Explain why a panel is empty: no session, modality not enabled, or nothing recorded yet. */
function emptyText(modality) {
  if (!session) return 'Select a session.';
  if (loadError) return loadError;
  const enabled = session.modalities[modality];
  if (enabled === false && !observations.some((item) => item.modality === modality)) {
    return `${MODALITY_LABEL[modality]} was not enabled for this session.`;
  }
  return session.activity === 'ended' || session.activity === 'unknown'
    ? 'Nothing was recorded for this modality.' : 'Nothing recorded yet; new observations appear here as SMPA writes them.';
}

/** Return the observations of one modality, newest first. */
function ofModality(modality) {
  return observations.filter((item) => item.modality === modality).slice().reverse();
}

/**
 * Draw the recorded visual evidence: descriptions with their frames, frames without descriptions, focused answers.
 */
function renderSee() {
  const items = ofModality('vision');
  dom.seeRecorded.replaceChildren(...(items.length ? items.map((item) => h('div', {class: 'observation', 'data-kind': item.kind, 'data-id': item.id},
    recordedLine(item),
    h('div', {class: 'muted', style: 'font-size:12px'}, KIND_LABEL[item.kind] || item.kind,
      item.provenance && item.provenance.question ? ` · question: ${item.provenance.question}` : ''),
    item.image ? h('img', {src: item.image, loading: 'lazy', alt: 'Camera frame recorded by SMPA'}) : null,
    item.text ? h('div', {class: 'observation-text'}, item.text) : null))
    : [h('p', {class: 'muted', 'data-empty': 'vision'}, emptyText('vision'))]));
}

/** Draw the recorded audio: one player and transcript per recording. */
function renderHear() {
  const items = ofModality('hear');
  dom.hearRecorded.replaceChildren(...(items.length ? items.map((item) => h('div', {class: 'observation', 'data-kind': item.kind, 'data-id': item.id},
    recordedLine(item),
    h('div', {class: 'muted', style: 'font-size:12px'},
      [item.provenance['Recognition model'], item.provenance.Language, item.provenance.duration_s ? `${fmt(item.provenance.duration_s, 1)} s` : null]
        .filter(Boolean).join(' · ')),
    item.audio ? h('audio', {controls: true, preload: 'none', src: item.audio}) : h('p', {class: 'muted'}, 'No recording file.'),
    h('div', {class: 'observation-text'}, item.text ? `“${item.text}”` : 'No transcript text.')))
    : [h('p', {class: 'muted', 'data-empty': 'hear'}, emptyText('hear'))]));
}

/**
 * Draw the recorded textual input: user messages SMPA's Sense stage observed.
 *
 * For a robot run that read a conversation, the bound conversation session
 * is named and can be selected. Text is labelled with its epistemic status:
 * it is what a user said, not a verified fact.
 */
function renderRead() {
  const items = ofModality('read');
  const binding = session && session.read_session;
  dom.readBinding.replaceChildren(binding
    ? h('span', {}, 'Reads conversation session ', binding.session_id
      ? h('a', {href: `#smpa/sense/${binding.session_id}`, id: 'read-binding'}, binding.session_id)
      : h('span', {class: 'mono'}, binding.path))
    : '');
  dom.readRecorded.replaceChildren(...(items.length ? items.map((item) => h('div', {class: 'observation', 'data-kind': item.kind, 'data-id': item.id},
    recordedLine(item),
    h('div', {class: 'muted', style: 'font-size:12px'}, item.provenance.role || 'user',
      item.provenance.message_index ? ` · message ${item.provenance.message_index}` : '',
      item.epistemic_status ? ` · ${item.epistemic_status.replace(/_/g, ' ')}` : ''),
    h('div', {class: 'observation-text'}, item.text || '')))
    : [h('p', {class: 'muted', 'data-empty': 'read'}, emptyText('read'))]));
}

/** Toggle the provenance details of one timeline entry. */
function toggle(id) {
  if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
  renderTimeline();
}

/**
 * Draw the unified timeline of every recorded observation and Sense invocation.
 *
 * Each row shows time, modality, kind, session, status and the first part of
 * the content; clicking it shows where it came from: source file, Sense
 * record, time source and every provenance field SMPA recorded.
 */
function renderTimeline() {
  let rows = observations.filter((item) => filters[item.modality]);
  if (filters.newestFirst) rows = rows.slice().reverse();
  dom.timelineCount.textContent = `${rows.length} of ${observations.length} entries` + (truncated ? ' (older entries omitted)' : '');
  dom.timeline.replaceChildren(...(rows.length ? rows.flatMap((item) => {
    const row = h('tr', {class: 'selectable', 'data-modality': item.modality, 'data-id': item.id, onclick: () => toggle(item.id)},
      h('td', {class: 'mono', style: 'white-space:nowrap'}, when(item)),
      h('td', {}, h('span', {class: `modality modality-${item.modality}`}, MODALITY_LABEL[item.modality])),
      h('td', {}, KIND_LABEL[item.kind] || item.kind),
      h('td', {class: 'mono', title: item.session_id}, item.session_id.slice(0, 8)),
      h('td', {}, item.status || ''),
      h('td', {}, (item.text || (item.image ? '[image]' : item.audio ? '[audio]' : '')).slice(0, 140)));
    if (!expanded.has(item.id)) return [row];
    const details = {source: item.source, sense_record: item.sense_record, cycle: item.cycle, timestamp: item.timestamp,
                     timestamp_source: item.timestamp_source, epistemic_status: item.epistemic_status, provenance: item.provenance};
    return [row, h('tr', {class: 'provenance-row'}, h('td', {colspan: 6}, h('pre', {}, JSON.stringify(details, null, 2))))];
  }) : [h('tr', {}, h('td', {colspan: 6, class: 'empty', id: 'timeline-empty'},
    session ? (loadError || 'No observations recorded in this session.') : 'Select a session.'))]));
}

/** Redraw every panel and the timeline. */
function renderAll() {
  renderSee();
  renderHear();
  renderRead();
  renderTimeline();
}

/**
 * Read the selected session's observations, redrawing only when the session changed on disk.
 *
 * An ended session is read once and then only rechecked rarely; a running
 * one is rechecked every two seconds. Errors keep the last data and say why.
 */
async function refreshObservations() {
  if (!session) return;
  const asked = session.session_id;
  try {
    const answer = await getJSON(`/smpa/sessions/${encodeURIComponent(asked)}/observations`);
    if (!session || session.session_id !== asked) return;
    loadError = null;
    if (answer.revision === revision) return;
    ({observations, revision, truncated} = answer);
    renderAll();
  } catch (error) {
    if (!session || session.session_id !== asked) return;
    loadError = `Observations could not be loaded: ${error.message}`;
    if (!observations.length) renderAll();
  }
}

/** Close the live camera stream and release its viewer. */
function stopCamera() {
  if (live.cameraClose) live.cameraClose();
  if (live.camera) live.camera.destroy();
  live.camera = live.cameraClose = live.cameraTopic = null;
}

/**
 * Show the session's camera topic live when it is published, and say clearly what it is.
 *
 * The camera topic comes from the session's configuration. The stream runs
 * only while this section is shown and only if the topic currently has a
 * publisher. For an ended session the label says that the picture is the
 * camera now, not part of the session.
 */
async function refreshCamera() {
  const topic = session && session.camera_topic;
  let published = false;
  if (topic) {
    try {
      published = (await getJSON('/sensors/topics')).topics.some((item) => item.topic === topic && item.publisher_count > 0);
    } catch (error) { published = false; }
  }
  if (!topic || !published) {
    stopCamera();
    dom.seeLiveBody.replaceChildren(h('p', {class: 'muted', id: 'live-camera-state'},
      !session ? '' : !topic ? 'This session names no camera topic.' : `No publisher on ${topic} right now.`));
    return;
  }
  if (live.cameraTopic === topic) return;
  stopCamera();
  dom.seeLiveBody.replaceChildren();
  const holder = h('div', {class: 'panel-body', id: 'live-camera', style: 'min-height:0'});
  const state = h('span', {class: 'muted'}, 'connecting…');
  dom.seeLiveBody.append(holder, h('div', {class: 'muted', style: 'font-size:12px'}, h('span', {class: 'mono'}, topic), ' · ', state));
  live.camera = imageViewer.create(holder);
  live.cameraTopic = topic;
  live.cameraClose = openStream('/sensors/stream', {topic, rate: 5, max_width: 640}, {
    onData: (header, binary) => { live.camera.update(header, binary); state.textContent = `${header.rate_hz} Hz`; },
    onStatus: (message) => { state.textContent = `${message.state}: ${message.detail || ''}`; },
  });
  dom.seeLiveNote.textContent = session.activity === 'ended'
    ? 'This is the camera now. The session has ended, so this picture is not part of it.'
    : 'Live ROS topic. SMPA records a frame only when its Sense stage runs; those frames are listed below.';
}

/** Close the live Listen status stream. */
function stopListen() {
  if (live.listenClose) live.listenClose();
  live.listenClose = live.listenTopic = null;
}

/**
 * Show whether the session's Listen action is executing right now.
 *
 * SMPA's Hear runs through a run-scoped ROS Listen action. Its standard
 * status topic says whether a goal is executing, which means SMPA is
 * listening. Only an active session can have one; otherwise the panel says
 * why nothing live is shown.
 */
function refreshListen() {
  const topics = session && session.ros ? session.ros.listen_status_topics : [];
  if (!session || session.activity !== 'active' || !topics.length) {
    stopListen();
    dom.hearLive.replaceChildren(h('span', {class: 'muted', id: 'live-listen'},
      !session ? '' : session.kind !== 'robot' ? 'Hear is not part of this kind of session.'
        : session.activity !== 'active' ? 'The session is not active, so nothing is heard live.'
          : 'No Listen action of this session is visible on ROS.'));
    return;
  }
  if (live.listenTopic === topics[0]) return;
  stopListen();
  const label = h('span', {class: 'pill', id: 'live-listen', 'data-state': 'unknown'}, 'LIVE · waiting for Listen status');
  dom.hearLive.replaceChildren(label, h('div', {class: 'muted mono', style: 'font-size:12px;word-break:break-all'}, topics[0]));
  live.listenTopic = topics[0];
  live.listenClose = openStream('/ros/topics/echo', {name: topics[0], type: 'action_msgs/msg/GoalStatusArray', rate: 4}, {
    onData: (message) => {
      const list = message.message.status_list;
      const states = Array.isArray(list) ? list.map((item) => item.status) : [];
      const listening = states.some((state) => state === 1 || state === 2);
      label.dataset.state = listening ? 'listening' : 'idle';
      label.className = 'pill ' + (listening ? 'pill-ok' : '');
      label.textContent = listening ? 'LIVE · listening now' : 'LIVE · Listen action idle';
    },
    onStatus: (status) => { label.dataset.state = status.state; label.textContent = `LIVE · ${status.state}`; },
  });
}

/** Switch to another session (or none), resetting what was shown for the previous one. */
function setSession(next) {
  const changed = (next && next.session_id) !== (session && session.session_id);
  session = next;
  if (changed) {
    observations = [];
    revision = null;
    truncated = false;
    loadError = null;
    expanded = new Set();
    renderAll();
    refreshObservations();
  }
  refreshListen();
  refreshCamera();
}

/** Build one panel with a heading, a live area and a recorded area. */
function panel(id, title, liveArea, recordedArea, note) {
  return h('section', {class: 'panel sense-panel', id},
    h('div', {class: 'panel-head'}, h('strong', {}, title)),
    liveArea ? h('div', {class: 'live-area'}, h('div', {class: 'live-badge'}, 'LIVE'), liveArea) : null,
    h('div', {class: 'recorded-area'}, h('div', {class: 'recorded-badge'}, 'RECORDED BY SMPA'), note || null, recordedArea));
}

/** Build one labelled check box bound to a timeline filter. */
function filterBox(key, label) {
  return h('label', {}, h('input', {type: 'checkbox', checked: filters[key], 'data-filter': key,
    onchange: (event) => { filters[key] = event.target.checked; renderTimeline(); }}), label);
}

export default {
  id: 'sense',
  title: 'Sense',

  /** Build the three panels and the timeline for a session and start refreshing. */
  mount(container, selected) {
    dom = {
      seeLiveBody: h('div', {}), seeLiveNote: h('p', {class: 'muted', style: 'font-size:12px;margin:4px 0'}),
      seeRecorded: h('div', {id: 'see-recorded'}), hearLive: h('div', {}), hearRecorded: h('div', {id: 'hear-recorded'}),
      readBinding: h('p', {class: 'muted', style: 'font-size:12px;margin:4px 0'}), readRecorded: h('div', {id: 'read-recorded'}),
      timeline: h('tbody', {id: 'timeline'}), timelineCount: h('span', {class: 'count', id: 'timeline-count'}),
    };
    container.append(h('div', {class: 'sense-view'},
      h('div', {class: 'sense-panels'},
        panel('see', 'See', h('div', {}, dom.seeLiveBody, dom.seeLiveNote), dom.seeRecorded),
        panel('hear', 'Hear', dom.hearLive, dom.hearRecorded),
        panel('read', 'Read', null, dom.readRecorded, h('div', {}, dom.readBinding,
          h('p', {class: 'muted', style: 'font-size:12px;margin:4px 0'},
            'Read-only. Text is entered on the Dialogue; SMPA records it, and it appears here.')))),
      h('section', {class: 'panel', id: 'timeline-panel'},
        h('div', {class: 'toolbar'}, h('strong', {}, 'Observation timeline'),
          filterBox('vision', 'See'), filterBox('hear', 'Hear'), filterBox('read', 'Read'), filterBox('sense', 'Sense records'),
          filterBox('newestFirst', 'Newest first'), dom.timelineCount),
        h('div', {style: 'overflow:auto;max-height:48vh'}, h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, 'Time'), h('th', {}, 'Modality'), h('th', {}, 'Kind'), h('th', {}, 'Session'),
            h('th', {}, 'Status'), h('th', {}, 'Content'))), dom.timeline)))));
    session = null;
    setSession(selected);
    stops = [
      poll(async () => { if (session && session.activity !== 'ended') await refreshObservations(); }, 2000),
      poll(async () => { if (session && session.activity === 'ended') await refreshObservations(); }, 15000),
      poll(refreshCamera, 3000),
    ];
  },

  /** Take the refreshed description of the selected session (activity may have changed). */
  update(next) { setSession(next); },

  /** Stop refreshing and close the live streams. */
  unmount() {
    for (const stop of stops) stop();
    stops = [];
    stopCamera();
    stopListen();
    session = null;
  },
};
