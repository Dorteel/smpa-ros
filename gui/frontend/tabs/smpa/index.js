/**
 * The SMPA tab: a session selector above four sections, Sense, Model, Plan and Act.
 *
 * Concepts: a session is one SMPA run, recorded by SMPA in its memory
 * directory and read (never written) by the GUI server. The shell lists the
 * sessions, keeps one selected, and hands it to the section that is shown.
 * A section is a module with mount(container, session) and unmount(), plus
 * an optional update(session) called whenever the session list refreshes.
 * Only Sense is functional in this iteration; the other sections explain
 * that they are not implemented yet.
 *
 * The address fragment "#smpa/<section>/<session id>" records the choice,
 * so reloading the page shows the same section and session.
 */
import {getJSON, h, poll} from '../../api.js';
import sense from './sense.js';

const ACTIVITY = {
  active: ['Active', 'pill-ok', 'Running; its ROS nodes are visible.'],
  running_not_visible: ['Running? not visible', 'pill-warn',
    'The metadata says running, but none of its ROS names are visible. The process may have stopped without finalizing its metadata, or it runs in another ROS domain or on another computer.'],
  running_unverified: ['Running (unverified)', 'pill-warn',
    'The metadata says running. SMPA offers no way to check whether this session is really alive (see SMPA_CONTRACTS.md, C1).'],
  ended: ['Ended', '', 'Finished; only recorded data is shown.'],
  unknown: ['Unknown', 'pill-off', 'No readable status.'],
};

/** A section that is reserved for a later iteration. */
function placeholder(title, content) {
  return {
    id: title.toLowerCase(),
    title,
    mount(container) {
      container.append(h('div', {class: 'empty', 'data-placeholder': title.toLowerCase()},
        h('p', {}, `${title} is not implemented in this iteration.`), h('p', {}, content)));
    },
    unmount() {},
  };
}

export const SECTIONS = [
  sense,
  placeholder('Model', 'It will show the world model snapshots and belief state of the selected session.'),
  placeholder('Plan', 'It will show goals and the actions the planner selected.'),
  placeholder('Act', 'It will show executed actions with their goals, feedback and results.'),
];

let dom = {};
let sessions = [];
let status = null;
let sectionId = 'sense';
let sessionId = null;
let mounted = null;
let stops = [];

/** Describe a session in one line for the selector. */
function sessionLabel(session) {
  const started = session.started_at ? session.started_at.replace('T', ' ').slice(0, 19) : 'no start time';
  const kind = {robot: 'robot', conversation: 'conversation', legacy_archive: 'archive', unknown: 'unreadable'}[session.kind];
  return `${started} · ${session.name} · ${kind} · ${(ACTIVITY[session.activity] || ACTIVITY.unknown)[0]}`;
}

/** Return the selected session, or null when none is selected or it no longer exists. */
export function selectedSession() {
  return sessions.find((session) => session.session_id === sessionId) || null;
}

/** Write the current section and session into the address without adding history entries. */
function remember() {
  const target = `#smpa/${sectionId}` + (sessionId ? `/${sessionId}` : '');
  if (location.hash !== target) history.replaceState(null, '', target);
}

/**
 * Show the details of the selected session above the sections.
 *
 * The badge explains the activity in its tooltip and in the line next to it,
 * so a stale "running" session is never presented as live.
 */
function renderSummary() {
  const session = selectedSession();
  if (!session) {
    dom.summary.replaceChildren(h('span', {class: 'muted'}, sessionId ? 'The selected session no longer exists.' : 'No session selected.'));
    return;
  }
  const [label, style, explanation] = ACTIVITY[session.activity] || ACTIVITY.unknown;
  const modes = Object.entries(session.modalities).filter(([, on]) => on).map(([name]) => name);
  dom.summary.replaceChildren(...[
    h('span', {class: `pill ${style}`, id: 'session-activity', 'data-activity': session.activity, title: explanation}, label),
    h('span', {class: 'mono', id: 'session-id', title: 'Session id'}, session.session_id),
    h('span', {class: 'muted'}, `${session.kind}` + (session.status ? ` · status ${session.status}` : '') +
      (session.completed_cycles !== null && session.completed_cycles !== undefined ? ` · ${session.completed_cycles} cycles` : '') +
      ` · senses: ${modes.length ? modes.join(', ') : 'none recorded as enabled'}`),
    h('span', {class: 'muted', id: 'session-explanation'}, explanation),
    session.metadata_error ? h('span', {class: 'notice', id: 'session-error'}, `metadata ${session.metadata_error}`) : null,
  ].filter(Boolean));
}

/** Refill the session selector, keeping the selection and marking a vanished selection. */
function renderSelector() {
  const options = [h('option', {value: ''}, sessions.length ? '— select a session —' : '— no sessions recorded —')];
  for (const session of sessions) options.push(h('option', {value: session.session_id}, sessionLabel(session)));
  if (sessionId && !selectedSession()) options.push(h('option', {value: sessionId}, `${sessionId} (no longer found)`));
  dom.select.replaceChildren(...options);
  dom.select.value = sessionId || '';
}

/** Mount the chosen section for the chosen session, replacing whatever was shown. */
function showSection() {
  if (mounted && mounted.unmount) mounted.unmount();
  dom.host.replaceChildren();
  mounted = SECTIONS.find((section) => section.id === sectionId) || SECTIONS[0];
  sectionId = mounted.id;
  for (const button of dom.sections.children) button.setAttribute('aria-selected', String(button.dataset.section === sectionId));
  mounted.mount(dom.host, selectedSession(), status);
  remember();
}

/**
 * Read the session list from the gateway and update everything that depends on it.
 *
 * Failure to reach the gateway shows the offline banner and keeps the last
 * list. A session that disappears stays selected and is reported as gone.
 */
async function refreshSessions() {
  try {
    const answer = await getJSON('/smpa/sessions');
    sessions = answer.sessions;
    dom.offline.style.display = 'none';
  } catch (error) {
    if (error.message.includes('No SMPA memory directory')) { sessions = []; dom.offline.style.display = 'none'; }
    else { dom.offline.style.display = ''; return; }
  }
  if (!sessionId && sessions.length) {
    sessionId = (sessions.find((session) => ['active', 'running_unverified'].includes(session.activity)) || sessions[0]).session_id;
    remember();
    showSection();
  }
  renderSelector();
  renderSummary();
  if (mounted && mounted.update) mounted.update(selectedSession());
}

/** Ask whether the memory directory is configured and whether it holds demonstration data. */
async function refreshStatus() {
  try {
    status = await getJSON('/smpa/status');
  } catch (error) {
    dom.offline.style.display = '';
    return;
  }
  dom.demo.style.display = status.demonstration ? '' : 'none';
  dom.unconfigured.style.display = status.configured ? 'none' : '';
  dom.root.textContent = status.memory_root ? `Recorded data from ${status.memory_root} (read-only)` : '';
}

/** Apply a sub-route "<section>/<session id>" from the address. */
function applyRoute(route) {
  const [section, ...rest] = (route || '').split('/');
  if (SECTIONS.some((item) => item.id === section)) sectionId = section;
  if (rest.join('/')) sessionId = rest.join('/');
}

export default {
  id: 'smpa',
  title: 'SMPA',

  /** Build the selector, banners and section bar, then start refreshing the session list. */
  mount(container, route) {
    applyRoute(route);
    dom = {
      select: h('select', {id: 'session-select', 'aria-label': 'SMPA session', onchange: (event) => {
        sessionId = event.target.value || null;
        renderSummary();
        showSection();
      }}),
      summary: h('div', {class: 'row', id: 'session-summary', style: 'margin:0'}),
      sections: h('nav', {class: 'subtabs', 'aria-label': 'SMPA sections'},
        SECTIONS.map((section) => h('button', {'data-section': section.id, role: 'tab',
          onclick: () => { sectionId = section.id; showSection(); }}, section.title))),
      host: h('div', {class: 'view', id: 'smpa-section'}),
      root: h('span', {class: 'muted', id: 'memory-root'}),
      demo: h('p', {class: 'warning', id: 'smpa-demo', style: 'display:none;margin:6px 0'},
        'DEMONSTRATION DATA: this memory directory was generated by the GUI test fixtures, not recorded by SMPA.'),
      unconfigured: h('p', {class: 'warning', id: 'smpa-unconfigured', style: 'display:none;margin:6px 0'},
        'No SMPA memory directory is configured. Start the GUI with --memory-root <smpa-ros>/memory ',
        '(or SMPA_GUI_MEMORY_ROOT). The ROS 2, Sensors and Actions tabs work without it.'),
      offline: h('p', {class: 'warning', id: 'smpa-offline', style: 'display:none;margin:6px 0'},
        'The GUI server cannot be reached. Showing the last loaded data.'),
    };
    container.append(
      h('div', {class: 'toolbar', style: 'flex-direction:column;align-items:stretch'},
        h('div', {class: 'row', style: 'margin:0'}, h('strong', {}, 'Session'), dom.select, dom.root),
        dom.summary, dom.demo, dom.unconfigured, dom.offline),
      dom.sections, dom.host);
    renderSelector();
    renderSummary();
    showSection();
    stops = [poll(refreshStatus, 10000), poll(refreshSessions, 3000)];
  },

  /** Follow address changes while the tab stays open. */
  navigate(route) {
    const before = [sectionId, sessionId].join('/');
    applyRoute(route);
    if ([sectionId, sessionId].join('/') !== before) { renderSelector(); renderSummary(); showSection(); }
  },

  /** Stop refreshing and unmount the shown section, which closes its streams. */
  unmount() {
    for (const stop of stops) stop();
    stops = [];
    if (mounted && mounted.unmount) mounted.unmount();
    mounted = null;
  },
};
