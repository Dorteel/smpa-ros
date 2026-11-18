/**
 * The Dialogue section: a conversation with SMPA on the left, its Sense, Belief and Plan graphs on the right.
 *
 * Concepts: a conversation is an SMPA conversation run. It is served by
 * SMPA's own conversation server, which the GUI server starts on demand and
 * relays to; inference, the transcript and the run record all belong to SMPA.
 * The active conversation is the run that server is serving now; it can be
 * continued. Any other conversation session is shown from its recorded
 * transcript and can be reopened. A graph view draws one Turtle file of the
 * selected session (real) or of the GUI's mock fixtures (mock, always marked
 * MOCK). The GUI never writes a transcript or a Turtle file itself.
 */
import {getJSON, h, poll, postJSON} from '../../api.js';
import {createRdfView, inspector} from './rdf_view.js';
import {chooseSession} from './index.js';

const VIEWS = [['sense', 'Sense'], ['belief', 'Belief'], ['plan', 'Plan']];
const PHASE = {
  stopped: ['Not running', '', 'SMPA\'s conversation server is started when you start or reopen a conversation.'],
  starting: ['Starting…', 'pill-warn', 'SMPA\'s conversation server is starting.'],
  ready: ['Ready', 'pill-ok', 'SMPA\'s conversation server is serving the active conversation.'],
  busy: ['Working…', 'pill-warn', 'SMPA is working on a request; it answers one request at a time.'],
  failed: ['Failed', 'pill-off', 'SMPA\'s conversation server stopped or could not start; see its output below.'],
  unreachable: ['Unreachable', 'pill-off', 'The configured external conversation server does not answer.'],
};
const OPERATION_TEXT = {start: 'starting a conversation', resume: 'reopening the conversation', model: 'switching the model',
  message: 'SMPA is generating a reply', stop: 'stopping the server'};
const STATE_TEXT = {
  missing: 'No file yet at this path. Nothing has been written for this view.',
  empty: 'The file exists but holds no triples.',
  malformed: 'The file is not valid Turtle right now (a writer may be midway).',
  unavailable: 'The file cannot be read.',
};

// Choices that survive re-mounting (the shell re-mounts sections when the session changes).
const prefs = {view: 'belief', source: 'real', model: null};
let session = null;
let dom = {};
let stops = [];
let conversation = null;
let models = [];
let modelProblems = [];
let access = {enabled: false, token: null};
let transcript = null;
let pending = null;
let notice = null;
let graphKey = null;
let graphShown = null;
let rdf = null;
let inspected = null;

/** Replace an element's children, skipping the null placeholders of optional parts. */
function fill(element, ...children) {
  element.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

/** Make a request id for a deliberate action, so a repeated request is not carried out twice. */
function requestId() {
  return 'gui' + Array.from(crypto.getRandomValues(new Uint8Array(12)), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** Tell whether the selected session is the conversation SMPA's server is serving now. */
function isActive() {
  return Boolean(conversation && conversation.run_id && session && conversation.run_id === session.session_id);
}

/** Return the model chosen in the selector, falling back to SMPA's configured default. */
function chosenModel() {
  return models.find((model) => model.source === prefs.model) || models.find((model) => model.default) || models[0] || null;
}

/** Describe a model's availability as a short badge text and style. */
function availabilityBadge(model) {
  const state = model.availability.state;
  const text = {available: 'available', likely: 'probably available', unavailable: 'unavailable', unknown: 'not checked'}[state] || state;
  return [text + (model.availability.verified ? '' : ' (precheck)'), {available: 'pill-ok', likely: 'pill-ok', unavailable: 'pill-off'}[state] || 'pill-warn'];
}

// ----- left panel: model, server, conversation ------------------------------------------------------

/** Draw the model selector with each model's provider, availability and description. */
function renderModels() {
  const chosen = chosenModel();
  fill(dom.model, ...models.map((model) => h('option', {value: model.source},
    `${model.name} · ${model.provider}${model.default ? ' · default' : ''}${model.availability.state === 'unavailable' ? ' · unavailable' : ''}`)));
  if (chosen) dom.model.value = chosen.source;
  if (!chosen) {
    fill(dom.modelInfo, h('p', {class: 'notice', id: 'model-problems'},
      modelProblems.join(' ') || 'No conversation models are configured in SMPA.'));
    return;
  }
  const [badge, style] = availabilityBadge(chosen);
  fill(dom.modelInfo,
    h('div', {class: 'row', style: 'margin:4px 0'},
      h('span', {class: `pill ${style}`, id: 'model-availability', 'data-state': chosen.availability.state}, badge),
      h('span', {class: 'mono muted', style: 'font-size:12px'}, chosen.source)),
    h('p', {class: 'muted', id: 'model-description', style: 'margin:2px 0;font-size:12.5px'}, chosen.description),
    chosen.availability.detail ? h('p', {class: 'muted', id: 'model-detail', style: 'margin:2px 0;font-size:12.5px'}, chosen.availability.detail) : null,
    modelProblems.length ? h('p', {class: 'notice', id: 'model-problems', style: 'padding:4px 0'}, modelProblems.join(' ')) : null);
}

/** Draw the server status line and its output log. */
function renderServer() {
  const phase = conversation ? conversation.phase : 'stopped';
  const [label, style, explanation] = PHASE[phase] || [phase, '', ''];
  const missing = conversation ? conversation.prerequisites_missing || [] : [];
  fill(dom.server,
    h('span', {class: `pill ${style}`, id: 'conversation-phase', 'data-phase': phase, title: explanation}, label),
    h('span', {class: 'muted', style: 'font-size:12px'}, conversation ? (conversation.mode === 'external' ? `external server ${conversation.url}` :
      `SMPA conversation server${conversation.pid ? ` (pid ${conversation.pid})` : ''}`) : ''),
    conversation && conversation.active_agent ? h('span', {class: 'muted', id: 'active-agent', style: 'font-size:12px'}, `· model ${conversation.active_agent.source}`) : null,
    conversation && conversation.operation ? h('span', {class: 'muted', id: 'conversation-progress', style: 'font-size:12px'},
      `· ${OPERATION_TEXT[conversation.operation.kind] || conversation.operation.kind} for ${Math.round(conversation.operation.elapsed_s)} s`) : null,
    conversation && conversation.mode === 'managed' && ['ready', 'failed'].includes(phase) && access.enabled
      ? h('button', {class: 'action', id: 'conversation-stop', onclick: stopServer, title: 'Stop SMPA\'s conversation server; SMPA finalizes the run.'}, 'Stop server') : null);
  dom.serverError.textContent = conversation && conversation.error ? conversation.error : '';
  dom.serverError.style.display = conversation && conversation.error ? '' : 'none';
  dom.prerequisites.style.display = missing.length ? '' : 'none';
  dom.prerequisites.textContent = missing.length ? 'Cannot start SMPA\'s conversation server. Missing: ' + missing.join('; ') : '';
  dom.log.textContent = conversation ? conversation.log.join('\n') : '';
}

/** Draw the buttons that start a new conversation or reopen the selected one. */
function renderControls() {
  const busy = conversation && conversation.phase === 'busy';
  const canWrite = access.enabled && !(conversation && conversation.prerequisites_missing && conversation.prerequisites_missing.length && conversation.mode === 'managed' && !isActive());
  const reopen = session && session.kind === 'conversation' && !isActive();
  fill(dom.controls,
    h('button', {class: 'primary', id: 'conversation-start', disabled: !canWrite || busy || !chosenModel(), onclick: startConversation,
      title: 'Ask SMPA to start a new conversation run with the selected model'}, 'Start new conversation'),
    reopen ? h('button', {class: 'action', id: 'conversation-resume', disabled: !canWrite || busy, onclick: resumeConversation,
      title: 'Reopen this conversation in SMPA so it can be continued'}, 'Continue this conversation') : null,
    isActive() && conversation.active_agent && chosenModel() && chosenModel().source !== conversation.active_agent.source
      ? h('button', {class: 'action', id: 'conversation-switch', disabled: !canWrite || busy, onclick: switchModel}, `Use ${chosenModel().name} from now on`) : null,
    access.enabled ? null : h('span', {class: 'muted', id: 'conversation-readonly'}, 'Read-only GUI: conversations can be viewed, not started or continued.'));
}

/** Describe what the message list shows: the live conversation, a recorded one, or nothing. */
function conversationHeading() {
  if (!session) return 'No session selected. Start a new conversation.';
  if (session.kind !== 'conversation') return 'The selected session is not a conversation. Start a new conversation or select a conversation session.';
  if (isActive()) return `Active conversation ${session.session_id}`;
  return `Recorded conversation ${session.session_id} (not active; continue it to reply)`;
}

/** Build one message bubble; text is shown as plain text, never as markup. */
function bubble(message, extraClass = '') {
  return h('div', {class: `message message-${message.role} ${extraClass}`, 'data-role': message.role, 'data-index': message.index ?? ''},
    h('div', {class: 'message-head'}, h('strong', {}, message.role === 'user' ? 'You' : 'SMPA'),
      message.agent ? h('span', {class: 'muted', title: message.agent.label || ''}, ` · ${message.agent.source || message.agent.model_id}`) : null,
      extraClass ? h('span', {class: 'muted'}, ' · sending…') : null),
    message.thinking ? h('details', {class: 'thinking'}, h('summary', {}, 'Reasoning'), h('div', {class: 'message-text'}, message.thinking)) : null,
    h('div', {class: 'message-text'}, message.content));
}

/** Draw the messages of the shown conversation, the pending message and any notice. */
function renderMessages() {
  dom.heading.textContent = conversationHeading();
  let messages = [];
  let problem = null;
  if (isActive()) messages = conversation.messages || [];
  else if (transcript && session && transcript.session_id === session.session_id) {
    messages = transcript.messages;
    if (transcript.state === 'missing') problem = `No transcript at ${transcript.configured_path} in this run.`;
    else if (transcript.state !== 'ok') problem = transcript.problem;
  }
  const atBottom = dom.messages.scrollHeight - dom.messages.scrollTop - dom.messages.clientHeight < 40;
  const items = messages.map((message) => bubble(message));
  if (pending && isActive() && !messages.some((message) => message.role === 'user' && message.content === pending.text && message.index > pending.after)) {
    items.push(bubble({role: 'user', content: pending.text, index: null}, 'pending'));
  }
  if (!items.length) items.push(h('p', {class: 'muted empty', id: 'no-messages'}, session && session.kind === 'conversation' ? 'No messages yet.' : ''));
  if (problem) items.push(h('p', {class: 'notice', id: 'transcript-problem'}, problem));
  if (notice) items.push(h('p', {class: notice.error ? 'warning' : 'muted', id: 'conversation-notice'}, notice.text));
  fill(dom.messages, ...items);
  if (atBottom || pending) dom.messages.scrollTop = dom.messages.scrollHeight;
  const ready = isActive() && conversation.phase === 'ready' && access.enabled;
  dom.input.disabled = !ready;
  dom.send.disabled = !ready || !dom.input.value.trim();
  dom.input.placeholder = ready ? 'Message SMPA (Enter sends, Shift+Enter for a new line)'
    : (isActive() && conversation.phase === 'busy' ? 'Waiting for SMPA…' : 'Start or continue a conversation to send messages');
}

/** Redraw the whole left panel. */
function renderLeft() {
  renderModels();
  renderServer();
  renderControls();
  renderMessages();
}

/**
 * Carry out one conversation request and show its outcome.
 *
 * The answer always includes the conversation status, also on failure, so
 * a saved message without a reply is shown together with SMPA's explanation.
 */
async function act(path, body, success) {
  notice = null;
  let answer;
  try {
    answer = await postJSON(path, body, access.token);
  } catch (error) {
    notice = {error: true, text: 'The GUI server could not be reached; the outcome is unknown. The conversation is refreshed automatically.'};
    renderLeft();
    return null;
  }
  if (answer.body.conversation) conversation = answer.body.conversation;
  if (!answer.ok) notice = {error: true, text: answer.body.detail || `The GUI server answered ${answer.status}`};
  else if (success) notice = {error: false, text: success};
  renderLeft();
  return answer;
}

/** Start a new conversation with the selected model and show it. */
async function startConversation() {
  const model = chosenModel();
  notice = {error: false, text: 'Starting a new conversation in SMPA…'};
  conversation = {...(conversation || {}), phase: 'starting', log: (conversation && conversation.log) || []};
  renderLeft();
  const answer = await act('/smpa/conversation/start', {source: model ? model.source : null, request_id: requestId()});
  if (answer && answer.ok && conversation.run_id) { notice = null; chooseSession(conversation.run_id); }
  refreshModels();
}

/** Reopen the selected recorded conversation so it can be continued. */
async function resumeConversation() {
  notice = {error: false, text: 'Reopening the conversation in SMPA…'};
  renderLeft();
  // The model recorded in the run is restored by SMPA; switching is a separate, explicit step.
  const answer = await act('/smpa/conversation/resume', {session_id: session.session_id, request_id: requestId()});
  if (answer && answer.ok) notice = null;
  refreshModels();
  renderLeft();
}

/** Switch the active conversation to the model chosen in the selector. */
async function switchModel() {
  await act('/smpa/conversation/model', {source: chosenModel().source}, `Model switched to ${chosenModel().source}.`);
}

/** Stop SMPA's conversation server; SMPA finalizes the run. */
async function stopServer() {
  await act('/smpa/conversation/stop', {}, 'SMPA\'s conversation server was stopped.');
  refreshModels();
}

/** Send the typed message once; keep the text if SMPA did not accept it. */
async function sendMessage() {
  const text = dom.input.value;
  if (!text.trim() || dom.send.disabled) return;
  pending = {text, after: (conversation.messages || []).length};
  dom.input.value = '';
  conversation = {...conversation, phase: 'busy'};
  renderLeft();
  const answer = await act('/smpa/conversation/messages', {text, request_id: requestId()});
  const saved = conversation && (conversation.messages || []).some((message) => message.role === 'user' && message.content === text && message.index > pending.after);
  if (!answer || (!answer.ok && !saved)) dom.input.value = dom.input.value || text;
  pending = null;
  renderLeft();
}

/** Refresh the conversation status (and the transcript of a recorded conversation). */
async function refreshConversation() {
  const answer = await getJSON('/smpa/conversation');
  conversation = answer.conversation;
  if (session && session.kind === 'conversation' && !isActive()) {
    try {
      transcript = await getJSON(`/smpa/sessions/${encodeURIComponent(session.session_id)}/conversation`);
    } catch (error) {
      transcript = {session_id: session.session_id, state: 'unavailable', messages: [], problem: error.message};
    }
  }
  renderServer();
  renderControls();
  renderMessages();
}

/** Refresh the model list; availability becomes SMPA's own verdict once its server runs. */
async function refreshModels() {
  const answer = await getJSON('/smpa/models');
  models = answer.models;
  modelProblems = answer.problems;
  renderModels();
  renderControls();
}

// ----- right panel: graphs -------------------------------------------------------------------------

/** Show the inspector for one node (or the hint when none is selected). */
function inspect(node) {
  inspected = node;
  fill(dom.inspector, ...inspector(node, node && node.kind !== 'edge' ? rdf.links(node.id) : null, (id) => rdf.focus(id)));
}

/** Draw the view and source switches. */
function renderGraphControls() {
  for (const button of dom.views.children) button.setAttribute('aria-selected', String(button.dataset.view === prefs.view));
  for (const button of dom.sources.children) button.setAttribute('aria-pressed', String(button.dataset.source === prefs.source));
  dom.mock.style.display = prefs.source === 'mock' ? '' : 'none';
  if (rdf) rdf.cy.resize();
}

/**
 * Fetch the current graph view and merge it into the drawing.
 *
 * Unchanged files (same revision) are not redrawn. A malformed or unreadable
 * file keeps the last valid drawing of the same view on screen and says so;
 * a missing or empty file clears the drawing and explains why.
 */
async function refreshGraph() {
  const key = [prefs.view, prefs.source, prefs.source === 'mock' ? '' : (session ? session.session_id : '')].join('|');
  if (key !== graphKey) { graphKey = key; graphShown = null; rdf.clear(); }
  if (prefs.source === 'real' && !session) {
    fill(dom.graphState, h('span', {class: 'muted', id: 'graph-state', 'data-state': 'no-session'}, 'Select a session to see its graphs.'));
    dom.overlay.textContent = 'Select a session to see its graphs.';
    dom.overlay.style.display = '';
    return;
  }
  const parameters = {source: prefs.source};
  if (prefs.source === 'real') parameters.session_id = session.session_id;
  const graph = await getJSON(`/smpa/graphs/${prefs.view}`, parameters);
  if (key !== graphKey) return;
  const keepLast = ['malformed', 'unavailable'].includes(graph.state) && graphShown;
  if (graph.state === 'ok' && (!graphShown || graphShown.revision !== graph.revision)) {
    rdf.show(graph);
    graphShown = graph;
    if (inspected && inspected.kind === 'edge') inspect(graph.edges.some((edge) => edge.id === inspected.edge.id) ? inspected : null);
    else if (inspected) inspect(graph.nodes.find((node) => node.id === inspected.id) || null);
  } else if (!keepLast && graph.state !== 'ok' && graphShown !== null) {
    rdf.clear();
    graphShown = null;
  }
  const shown = graph.state === 'ok' ? graph : graphShown;
  dom.overlay.style.display = shown ? 'none' : '';
  dom.overlay.textContent = shown ? '' : (STATE_TEXT[graph.state] || graph.state) + (graph.error ? ` ${graph.error}` : '');
  const [label, style] = {ok: ['up to date', 'pill-ok'], missing: ['no file', 'pill-warn'], empty: ['empty', 'pill-warn'],
    malformed: ['malformed', 'pill-off'], unavailable: ['unreadable', 'pill-off']}[graph.state] || [graph.state, ''];
  fill(dom.graphState,
    h('span', {class: `pill ${style}`, id: 'graph-state', 'data-state': graph.state, 'data-revision': graph.revision || ''}, label),
    h('span', {class: 'count', id: 'graph-count'}, shown ? `${shown.nodes.length} nodes · ${shown.edges.length} edges · ${shown.triples} triples` : ''),
    shown && shown.truncated ? h('span', {class: 'notice', style: 'padding:0'}, 'truncated') : null,
    h('span', {class: 'mono muted', id: 'graph-path', style: 'font-size:12px;word-break:break-all', title: graph.path}, graph.configured_path),
    graph.fallback ? h('span', {class: 'muted', id: 'graph-fallback', style: 'font-size:12px',
      title: `Preferred location: ${graph.candidates[0]}`}, `(SMPA's current location; preferred: ${graph.candidates[0]})`) : null,
    keepLast ? h('span', {class: 'notice', id: 'graph-stale', style: 'padding:0', title: graph.error || ''},
      `Showing the last valid version; the file is ${graph.state} now.`) : null,
    graph.modified_at ? h('span', {class: 'muted', style: 'font-size:12px'}, `changed ${new Date(graph.modified_at).toLocaleTimeString([], {hour12: false})}`) : null);
}

/** Switch the graph view or source and redraw right away. */
function choose(change) {
  Object.assign(prefs, change);
  renderGraphControls();
  refreshGraph().catch(() => {});
}

/**
 * Replace the Dialogue with a note when SMPA integration is disabled in this GUI.
 *
 * Polling stops, because every Dialogue route then answers that SMPA is
 * disabled. The rest of the GUI is unaffected.
 */
function showDisabled(container) {
  for (const stop of stops) stop();
  stops = [];
  if (rdf) { rdf.destroy(); rdf = null; }
  container.replaceChildren(h('div', {class: 'empty', id: 'dialogue-disabled'},
    h('p', {}, 'SMPA integration is disabled in this GUI (started with --no-smpa or smpa:=false).'),
    h('p', {}, 'The conversation and the Sense, Belief and Plan graphs are not available. The ROS 2, Sensors and Actions tabs are unaffected.')));
}

/**
 * Say which path answers messages: SMPA's conversation server, or the ROS 2 stages Sense, Model, Plan and Act.
 *
 * The GUI server reports which stage interfaces are still pending. This
 * page keeps sending messages through the conversation server, as before,
 * until the stage interfaces exist and the page is switched over.
 */
async function refreshPath() {
  const answer = (await getJSON('/smpa/dialogue')).dialogue;
  dom.path.dataset.path = answer.path;
  dom.path.textContent = answer.ros_path_ready
    ? 'Replies: SMPA\'s conversation server. The ROS 2 stage path (Sense → Model → Plan → Act) is configured; '
      + 'turns over it are started through the API (POST /api/v1/smpa/dialogue/turns).'
    : `Replies: SMPA's conversation server. ROS 2 stage path pending (${answer.pending_stages.join(', ')}).`;
}

export default {
  id: 'dialogue',
  title: 'Dialogue',

  /** Build both columns and start refreshing the conversation, the models and the graph. */
  mount(container, selected) {
    session = selected;
    transcript = null;
    notice = null;
    graphKey = null;
    dom = {
      model: h('select', {id: 'model-select', 'aria-label': 'Conversation model', onchange: (event) => { prefs.model = event.target.value; renderModels(); renderControls(); }}),
      modelInfo: h('div', {id: 'model-info'}),
      server: h('div', {class: 'row', id: 'conversation-server', style: 'margin:0'}),
      serverError: h('p', {class: 'warning', id: 'conversation-error', style: 'display:none'}),
      prerequisites: h('p', {class: 'warning', id: 'conversation-prerequisites', style: 'display:none'}),
      log: h('pre', {id: 'conversation-log', style: 'max-height:180px;margin:4px 0'}),
      path: h('p', {class: 'muted', id: 'dialogue-path', style: 'font-size:12px;margin:4px 0'}),
      controls: h('div', {class: 'row', id: 'conversation-controls', style: 'margin:6px 0'}),
      heading: h('div', {class: 'muted', id: 'conversation-heading', style: 'font-size:12.5px;padding:6px 12px;border-bottom:1px solid var(--line)'}),
      messages: h('div', {class: 'messages', id: 'messages'}),
      input: h('textarea', {id: 'message-input', rows: 3, class: 'composer-input',
        oninput: () => { dom.send.disabled = dom.input.disabled || !dom.input.value.trim(); },
        onkeydown: (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); } }}),
      send: h('button', {class: 'primary', id: 'message-send', onclick: sendMessage}, 'Send'),
      views: h('nav', {class: 'subtabs', 'aria-label': 'Graph views', style: 'padding:0;border:0;background:none'},
        VIEWS.map(([id, title]) => h('button', {'data-view': id, role: 'tab', onclick: () => choose({view: id})}, title))),
      sources: h('div', {class: 'segmented', role: 'group', 'aria-label': 'Graph source'},
        h('button', {'data-source': 'real', onclick: () => choose({source: 'real'}), title: 'Files in the selected session\'s run directory'}, 'Real'),
        h('button', {'data-source': 'mock', onclick: () => choose({source: 'mock'}), title: 'GUI test fixtures; not SMPA data'}, 'Mock')),
      mock: h('p', {class: 'warning mock-banner', id: 'graph-mock', style: 'display:none;margin:0'},
        'MOCK DATA: GUI test fixtures, not observations, beliefs or plans recorded by SMPA.'),
      graphState: h('div', {class: 'row', id: 'graph-status', style: 'margin:0;gap:8px'}),
      canvas: h('div', {id: 'rdf-canvas'}),
      overlay: h('div', {class: 'graph-overlay', id: 'graph-overlay'}),
      inspector: h('aside', {class: 'graph-side', id: 'graph-inspector'}),
    };
    const left = h('section', {class: 'dialogue-left'},
      h('div', {class: 'pad', style: 'border-bottom:1px solid var(--line)'},
        h('label', {class: 'stack'}, h('strong', {}, 'Model'), dom.model), dom.modelInfo, dom.controls, dom.server,
        dom.serverError, dom.prerequisites, dom.path,
        h('details', {}, h('summary', {class: 'muted', style: 'font-size:12px'}, 'Server output'), dom.log)),
      dom.heading, dom.messages,
      h('div', {class: 'composer'}, dom.input, dom.send));
    const right = h('section', {class: 'dialogue-right'},
      h('div', {class: 'toolbar'}, dom.views, dom.sources,
        h('button', {class: 'action', onclick: () => rdf.fit()}, 'Fit'),
        h('button', {class: 'action', onclick: () => rdf.arrange()}, 'Re-arrange'), dom.graphState),
      dom.mock,
      h('div', {class: 'graph-area'}, h('div', {class: 'graph-stage'}, dom.canvas, dom.overlay), dom.inspector));
    container.append(h('div', {class: 'dialogue'}, left, right));
    rdf = createRdfView(dom.canvas, inspect);
    window.__rdf = rdf.cy;
    inspect(null);
    renderGraphControls();
    renderLeft();
    getJSON('/actions/session').then((answer) => { access = answer; renderLeft(); }).catch(() => { access = {enabled: false, token: null}; renderLeft(); });
    stops = [poll(refreshConversation, 1500, (error) => {
      if (error.message.includes('SMPA integration is disabled')) { showDisabled(container); return; }
      dom.serverError.style.display = '';
      dom.serverError.textContent = 'The GUI server cannot be reached.';
    }), poll(refreshModels, 15000, () => {}), poll(refreshGraph, 1500, () => {}), poll(refreshPath, 15000, () => {})];
  },

  /** Follow the shell's session list: the selected session's details may have changed. */
  update(selected) {
    const changed = (selected && selected.session_id) !== (session && session.session_id);
    session = selected;
    if (changed) { transcript = null; refreshGraph().catch(() => {}); }
    renderControls();
    renderMessages();
  },

  /** Stop refreshing and release the drawing. */
  unmount() {
    for (const stop of stops) stop();
    stops = [];
    if (rdf) rdf.destroy();
    rdf = null;
    window.__rdf = null;
  },
};
