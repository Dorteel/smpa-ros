/**
 * The Actions tab: discover action servers, edit a goal, send it, and watch what happens.
 *
 * Concepts: a goal is sent only by the confirmation button in the dialog
 * that the Send Goal button opens. Nothing else in this tab sends anything:
 * selecting a server, editing values, loading a saved configuration,
 * re-using values from the history, reloading the page and reconnecting
 * after a failure only read from the gateway.
 *
 * The gateway keeps the record of every goal it sent (state, feedback,
 * result). This tab shows those records by polling, so the display survives
 * page reloads and connection losses without the browser remembering goals.
 * A goal submission carries a request identifier created for that one click;
 * the gateway refuses to send the same identifier twice.
 */
import {getJSON, h, poll, postJSON} from '../../api.js';
import {createEditor, validate, withDefaults} from './form.js';
import {configurationNames, deleteConfiguration, loadConfiguration, saveConfiguration} from './store.js';

const ACTIVE = ['sending', 'accepted', 'executing', 'canceling'];
const interfaces = new Map();
const drafts = new Map();
let session = {enabled: false, token: null, cancel_on_exit: true};
let servers = [];
let selected = null;
let described = null;
let editor = null;
let mode = 'form';
let goals = [];
let selectedGoalId = null;
let shownGoal = '';
let shownHistory = '';
let stops = [];
let dom = {};

/** Format a time stamp in seconds as local clock time with milliseconds, or a dash. */
function clock(seconds) {
  if (!seconds) return '–';
  const date = new Date(seconds * 1000);
  return date.toLocaleTimeString([], {hour12: false}) + '.' + String(date.getMilliseconds()).padStart(3, '0');
}

/** Show a message under the editor; kind "error" is red, "note" is neutral, empty text clears it. */
function notify(text, kind = 'note') {
  dom.notice.textContent = text;
  dom.notice.className = kind === 'error' ? 'notice' : 'muted';
  dom.notice.style.color = kind === 'error' ? 'var(--bad)' : '';
}

/** List field problems under the editor and mark the affected inputs. */
function showErrors(errors) {
  dom.errors.replaceChildren(...errors.map((error) => h('li', {}, h('span', {class: 'mono'}, error.path || 'goal'), ' ', error.message)));
  if (editor && mode === 'form') editor.showErrors(errors);
  dom.json.classList.toggle('invalid', mode === 'json' && errors.length > 0);
}

/**
 * Redraw the list of discovered action servers.
 *
 * Each row shows the action name, its type and whether a server is
 * currently available. Clicking a row selects the action for editing; it
 * does not send anything.
 */
function renderServers() {
  const text = dom.filter.value.trim().toLowerCase();
  const rows = servers.filter((action) => !text || action.name.toLowerCase().includes(text) || action.types.join(' ').toLowerCase().includes(text))
    .map((action) => h('tr', {class: 'selectable' + (selected && selected.name === action.name ? ' selected' : ''), 'data-action': action.name,
      onclick: () => selectAction(action.name, action.types[0])},
      h('td', {}, h('div', {class: 'mono', style: 'word-break:break-all'}, action.name),
        h('div', {class: 'mono muted', style: 'word-break:break-all'}, action.types.join(', ')),
        h('span', {class: 'pill ' + (action.available ? 'pill-ok' : 'pill-off')}, action.available ? 'available' : 'no server'))));
  dom.servers.replaceChildren(...(rows.length ? rows : [h('tr', {}, h('td', {class: 'empty'}, 'No action servers discovered.'))]));
  updateControls();
}

/** Return the discovered entry of the selected action, or null when it is not currently discovered. */
function selectedServer() {
  return selected ? servers.find((action) => action.name === selected.name) || null : null;
}

/**
 * Enable or disable the Send and Cancel buttons to match the current situation.
 *
 * Sending needs action execution to be enabled, a selected action and an
 * available server. Cancelling needs a selected goal that is still active.
 * The reason for a disabled Send button is shown next to it.
 */
function updateControls() {
  const server = selectedServer();
  const reason = !session.enabled ? 'Action execution is disabled (read-only mode).'
    : !selected ? 'Select an action server.'
    : !described ? 'Loading the action interface…'
    : !server || !server.available ? 'No server is currently available for this action.' : '';
  dom.send.disabled = reason !== '';
  dom.sendReason.textContent = reason;
  const goal = goals.find((item) => item.goal_id === selectedGoalId);
  dom.cancel.disabled = !session.enabled || !goal || !ACTIVE.includes(goal.state) || goal.state === 'sending';
}

/**
 * Select an action and show an editor for its Goal definition.
 *
 * The values being edited for the previously selected action are kept as a
 * draft for this page visit. The interface description is fetched from the
 * gateway (once per type) and the editor starts from the draft or from the
 * interface's default values. This function only reads; it never sends a goal.
 */
async function selectAction(name, type) {
  rememberDraft();
  selected = {name, type};
  described = null;
  editor = null;
  dom.editorTitle.textContent = name;
  dom.editorType.textContent = type;
  dom.form.replaceChildren(h('p', {class: 'muted'}, 'Loading the action interface…'));
  showErrors([]);
  notify('');
  renderServers();
  try {
    if (!interfaces.has(type)) interfaces.set(type, await getJSON('/actions/interface', {type}));
  } catch (error) {
    if (selected && selected.name === name) dom.form.replaceChildren(h('p', {class: 'notice'}, error.message));
    return;
  }
  if (!selected || selected.name !== name) return;
  described = interfaces.get(type);
  const start = drafts.has(name) ? drafts.get(name) : described.goal_default;
  editor = createEditor(described.goal, withDefaults(described.goal, start, described.goal_default), () => showErrors([]));
  dom.form.replaceChildren(editor.element);
  dom.json.value = JSON.stringify(editor.get(), null, 2);
  setMode(mode);
  renderConfigurations();
  updateControls();
}

/** Keep the values currently in the editor as the draft of the selected action. */
function rememberDraft() {
  if (!selected || !editor) return;
  const current = readValue();
  if (current.value !== undefined) drafts.set(selected.name, current.value);
}

/**
 * Read the goal value from whichever editor is shown.
 *
 * In form mode this is the form's value. In JSON mode the text is parsed;
 * text that is not valid JSON gives an error entry instead of a value.
 */
function readValue() {
  if (mode === 'form') return {value: editor.get(), errors: []};
  try {
    return {value: JSON.parse(dom.json.value), errors: []};
  } catch (error) {
    return {value: undefined, errors: [{path: '', message: 'is not valid JSON: ' + error.message}]};
  }
}

/**
 * Switch between the generated form and the JSON text editor.
 *
 * The value moves with the switch. Leaving JSON mode needs valid JSON that
 * is an object; otherwise the switch is refused and the problem is shown,
 * so no edits are lost.
 */
function setMode(next) {
  if (editor && next !== mode) {
    const current = readValue();
    if (current.value === undefined || (next === 'form' && (current.value === null || typeof current.value !== 'object' || Array.isArray(current.value)))) {
      showErrors(current.errors.length ? current.errors : [{path: '', message: 'must be a JSON object to be shown as a form'}]);
      return;
    }
    if (next === 'form') editor.set(withDefaults(described.goal, current.value, described.goal_default));
    else dom.json.value = JSON.stringify(current.value, null, 2);
    showErrors([]);
  }
  mode = next;
  dom.form.style.display = mode === 'form' ? '' : 'none';
  dom.json.style.display = mode === 'json' ? '' : 'none';
  dom.modeForm.setAttribute('aria-selected', String(mode === 'form'));
  dom.modeJson.setAttribute('aria-selected', String(mode === 'json'));
}

/** Put a goal value into the editor that is currently shown; this never sends it. */
function putValue(value) {
  const filled = withDefaults(described.goal, value, described.goal_default);
  editor.set(filled && typeof filled === 'object' && !Array.isArray(filled) ? filled : described.goal_default);
  dom.json.value = JSON.stringify(filled, null, 2);
  showErrors([]);
}

/** Refresh the list of saved configurations for the selected action's type. */
function renderConfigurations() {
  const names = selected ? configurationNames(selected.type) : [];
  dom.configSelect.replaceChildren(h('option', {value: ''}, names.length ? '— saved configurations —' : '— none saved —'),
    ...names.map((name) => h('option', {value: name}, name)));
}

/**
 * Check the edited goal and, if it fits the action's definition, open the confirmation dialog.
 *
 * Problems are listed and nothing further happens. Otherwise the dialog
 * names the action, the server nodes and the exact values, and warns that a
 * robot may move. The goal is sent only when the user confirms there.
 */
function askToSend() {
  if (!editor || !described) return;
  const current = readValue();
  const errors = current.errors.length ? current.errors : validate(described.goal, current.value);
  showErrors(errors);
  if (errors.length) { notify('The goal was not sent: fix the listed fields first.', 'error'); return; }
  notify('');
  const server = selectedServer();
  dom.dialogAction.textContent = `${selected.name}  (${selected.type})`;
  dom.dialogServer.textContent = server && server.servers.length ? server.servers.join(', ') : 'unknown';
  dom.dialogGoal.textContent = JSON.stringify(current.value, null, 2);
  dom.confirm.disabled = false;
  dom.dialog.returnValue = '';
  dom.dialog.pendingGoal = {action: selected.name, type: selected.type, goal: current.value};
  dom.dialog.showModal();
}

/**
 * Send the confirmed goal exactly once and select it in the monitor.
 *
 * A fresh request identifier is created for this confirmation. The request
 * is not retried. If the gateway refuses the goal, its explanation and any
 * field problems are shown. If the network fails, the user is told that the
 * outcome is unknown and to check the history before trying again, because
 * the goal may have been sent.
 */
async function sendConfirmed() {
  const pending = dom.dialog.pendingGoal;
  dom.dialog.pendingGoal = null;
  dom.confirm.disabled = true;
  dom.dialog.close();
  if (!pending) return;
  const requestId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/-/g, '');
  let answer;
  try {
    answer = await postJSON('/actions/goals', {...pending, request_id: requestId}, session.token);
  } catch (error) {
    notify('The request did not complete. It is not known whether the goal was sent: check the history before sending again.', 'error');
    return;
  }
  if (!answer.ok) {
    showErrors(answer.body.errors || []);
    notify(`The goal was not sent: ${typeof answer.body.detail === 'string' ? answer.body.detail : 'the gateway refused the request'}`, 'error');
    if (answer.status === 403) refreshSession();
    return;
  }
  selectedGoalId = answer.body.goal.goal_id;
  notify(`Goal ${selectedGoalId} sent at ${clock(answer.body.goal.created_at)}.`);
  await refreshGoals();
}

/** Ask the gateway to request cancellation of the selected goal; done once, on click. */
async function cancelSelected() {
  if (!selectedGoalId) return;
  try {
    const answer = await postJSON(`/actions/goals/${selectedGoalId}/cancel`, {}, session.token);
    notify(answer.ok ? 'Cancellation requested; the action server decides whether and when the goal stops.'
      : `Cancellation was not requested: ${answer.body.detail || 'the gateway refused the request'}`, answer.ok ? 'note' : 'error');
  } catch (error) {
    notify('The cancel request did not complete; check the goal state and try again if it is still active.', 'error');
  }
  await refreshGoals();
}

/** Build one labelled line of the goal monitor. */
function line(label, value, id) {
  return [h('dt', {}, label), h('dd', {id}, value)];
}

/**
 * Redraw the monitor for the selected goal: state, identifiers, times, feedback and result.
 *
 * The monitor is rebuilt only when the goal's record changed, so text
 * selections and scroll positions survive the frequent polling.
 */
function renderMonitor() {
  const goal = goals.find((item) => item.goal_id === selectedGoalId);
  const signature = JSON.stringify(goal || null);
  if (signature === shownGoal) return;
  shownGoal = signature;
  if (!goal) {
    dom.monitor.replaceChildren(h('p', {class: 'muted'}, 'No goal selected. Send a goal or pick one from the history.'));
    return;
  }
  const cancel = goal.cancel_requested_at ? `requested ${clock(goal.cancel_requested_at)} · server answer: ${goal.cancel_answer}` : 'not requested';
  dom.monitor.replaceChildren(
    h('div', {class: 'row'}, h('span', {class: 'state', id: 'goal-state', 'data-state': goal.state}, goal.state),
      h('span', {class: 'mono'}, goal.action)),
    h('p', {class: 'notice', id: 'goal-error', style: goal.error ? '' : 'display:none'}, goal.error || ''),
    h('dl', {class: 'readout', style: 'padding:0'},
      ...line('Goal UUID', goal.goal_id, 'goal-uuid'), ...line('Type', goal.type),
      ...line('Sent', clock(goal.created_at)), ...line('Accepted', clock(goal.accepted_at)),
      ...line('Finished', clock(goal.finished_at)), ...line('Cancellation', cancel, 'goal-cancel')),
    h('h3', {}, `Feedback (${goal.feedback_count} received` + (goal.feedback_count > goal.feedback.length ? `, last ${goal.feedback.length} shown` : '') + ')'),
    h('div', {class: 'feedback-list', id: 'goal-feedback'}, goal.feedback.length
      ? goal.feedback.slice().reverse().map((item) => h('div', {}, `#${item.number}  ${clock(item.at)}  ${JSON.stringify(item.message)}`))
      : h('div', {class: 'muted'}, 'No feedback received.')),
    h('h3', {}, 'Result' + (goal.result_truncated ? ' (long fields shortened)' : '')),
    h('pre', {id: 'goal-result'}, goal.result === null ? (ACTIVE.includes(goal.state) ? 'Waiting for the result…' : 'No result.') : JSON.stringify(goal.result, null, 2)),
    h('h3', {}, 'Goal values'), h('pre', {id: 'goal-values'}, JSON.stringify(goal.goal, null, 2)),
    h('button', {class: 'action', id: 'reuse-goal', type: 'button', onclick: () => reuse(goal)}, 'Copy these values into the editor (does not send)'));
}

/** Load a history entry's action and values into the editor for editing; nothing is sent. */
async function reuse(goal) {
  drafts.set(goal.action, goal.goal);
  if (selected && selected.name === goal.action && editor) putValue(goal.goal);
  else { editor = null; await selectAction(goal.action, goal.type); }
  notify('Values copied into the editor. Nothing was sent.');
}

/** Redraw the history table when the list of goals or their states changed. */
function renderHistory() {
  const signature = JSON.stringify(goals.map((goal) => [goal.goal_id, goal.state, goal.feedback_count])) + selectedGoalId;
  if (signature === shownHistory) return;
  shownHistory = signature;
  dom.history.replaceChildren(...(goals.length ? goals.map((goal) => h('tr', {
    class: 'selectable' + (goal.goal_id === selectedGoalId ? ' selected' : ''), 'data-goal': goal.goal_id, 'data-state': goal.state,
    onclick: () => { selectedGoalId = goal.goal_id; renderGoals(); }},
    h('td', {}, clock(goal.created_at)), h('td', {class: 'mono'}, goal.action),
    h('td', {}, h('span', {class: 'state', 'data-state': goal.state}, goal.state)),
    h('td', {class: 'mono', title: goal.goal_id}, goal.goal_id.slice(0, 8)), h('td', {class: 'mono'}, JSON.stringify(goal.goal).slice(0, 60))))
    : [h('tr', {}, h('td', {class: 'empty', colspan: 5}, 'No goals have been sent since the GUI server started.'))]));
}

/** Redraw everything that depends on the goal records. */
function renderGoals() {
  renderHistory();
  renderMonitor();
  updateControls();
}

/** Read the goal records from the gateway and redraw; reading never sends anything. */
async function refreshGoals() {
  goals = (await getJSON('/actions/goals')).goals;
  dom.offline.style.display = 'none';
  renderGoals();
}

/** Ask the gateway whether action execution is enabled and fetch the current session token. */
async function refreshSession() {
  try {
    session = await getJSON('/actions/session');
  } catch (error) {
    session = {enabled: false, token: null, cancel_on_exit: true};
  }
  dom.readOnly.style.display = session.enabled ? 'none' : '';
  dom.exitPolicy.textContent = session.cancel_on_exit
    ? 'If the GUI server is stopped normally, it asks the servers to cancel goals that are still active.'
    : 'If the GUI server is stopped, active goals keep running on their servers.';
  updateControls();
}

export default {
  id: 'actions',
  title: 'Actions',

  /**
   * Build the three areas (servers, goal editor, monitor with history) and start reading.
   *
   * Mounting only reads: it fetches the session, the server list and the
   * goal records. If an action was selected earlier in this page visit its
   * editor is restored with its draft values, without sending anything.
   */
  mount(container) {
    dom = {
      filter: h('input', {type: 'search', placeholder: 'Filter actions…', style: 'flex:1', oninput: renderServers}),
      servers: h('tbody', {id: 'action-server-list'}),
      editorTitle: h('h2', {id: 'editor-action'}, 'No action selected'),
      editorType: h('p', {class: 'mono muted', id: 'editor-type'}, ''),
      form: h('div', {id: 'goal-editor'}, h('p', {class: 'muted'}, 'Select an action server to edit a goal.')),
      json: h('textarea', {id: 'goal-json', spellcheck: 'false', style: 'display:none', oninput: () => showErrors([])}),
      modeForm: h('button', {id: 'mode-form', type: 'button', role: 'tab', onclick: () => setMode('form')}, 'Form'),
      modeJson: h('button', {id: 'mode-json', type: 'button', role: 'tab', onclick: () => setMode('json')}, 'JSON'),
      errors: h('ul', {class: 'error-list', id: 'goal-errors'}),
      notice: h('p', {class: 'muted', id: 'action-notice'}, ''),
      send: h('button', {class: 'primary', id: 'send-goal', type: 'button', disabled: true, onclick: askToSend}, 'Send Goal…'),
      sendReason: h('span', {class: 'muted', id: 'send-reason'}, ''),
      cancel: h('button', {class: 'danger', id: 'cancel-goal', type: 'button', disabled: true, onclick: cancelSelected}, 'Cancel Goal'),
      configSelect: h('select', {id: 'config-select'}),
      configName: h('input', {type: 'text', id: 'config-name', placeholder: 'Configuration name'}),
      monitor: h('div', {id: 'goal-monitor'}),
      history: h('tbody', {id: 'goal-history'}),
      readOnly: h('p', {class: 'warning', id: 'read-only-notice', style: 'display:none'},
        'Action execution is disabled or not available from this address. Goals can be inspected but not sent.'),
      offline: h('p', {class: 'warning', id: 'actions-offline', style: 'display:none'},
        'The GUI server cannot be reached. Goals already sent keep running on their action servers; nothing is resent when the connection returns.'),
      exitPolicy: h('span', {id: 'exit-policy'}, ''),
      dialogAction: h('dd', {class: 'mono'}), dialogServer: h('dd', {class: 'mono'}), dialogGoal: h('pre', {id: 'confirm-values'}),
      confirm: h('button', {class: 'primary', id: 'confirm-send', type: 'button', onclick: sendConfirmed}, 'Send goal now'),
    };
    dom.dialog = h('dialog', {id: 'confirm-dialog'},
      h('h2', {}, 'Send this action goal?'),
      h('p', {class: 'warning'}, 'This sends a real goal to a running action server. If that server controls a robot or other hardware, ',
        'it may start moving immediately. Make sure the surroundings are safe and that you can stop it.'),
      h('dl', {class: 'readout', style: 'padding:0'}, h('dt', {}, 'Action'), dom.dialogAction, h('dt', {}, 'Server node'), dom.dialogServer),
      dom.dialogGoal,
      h('div', {class: 'row'}, dom.confirm,
        h('button', {class: 'action', id: 'decline-send', type: 'button', onclick: () => { dom.dialog.pendingGoal = null; dom.dialog.close(); }}, 'Do not send')));
    dom.dialog.addEventListener('cancel', () => { dom.dialog.pendingGoal = null; });

    const configurations = h('div', {class: 'row'},
      dom.configSelect,
      h('button', {class: 'action', id: 'config-load', type: 'button', onclick: () => {
        const values = selected && dom.configSelect.value ? loadConfiguration(selected.type, dom.configSelect.value) : null;
        if (values === null || !editor) return;
        putValue(values);
        notify(`Configuration "${dom.configSelect.value}" loaded into the editor. Nothing was sent.`);
      }}, 'Load'),
      h('button', {class: 'action', id: 'config-delete', type: 'button', onclick: () => {
        if (!selected || !dom.configSelect.value) return;
        deleteConfiguration(selected.type, dom.configSelect.value);
        renderConfigurations();
      }}, 'Delete'),
      dom.configName,
      h('button', {class: 'action', id: 'config-save', type: 'button', onclick: () => {
        const name = dom.configName.value.trim();
        const current = editor ? readValue() : {value: undefined};
        if (!selected || !name || current.value === undefined) { notify('Enter a name and valid goal values to save a configuration.', 'error'); return; }
        notify(saveConfiguration(selected.type, name, current.value) ? `Configuration "${name}" saved in this browser.`
          : 'The browser refused to store the configuration.', 'note');
        renderConfigurations();
        dom.configSelect.value = name;
      }}, 'Save'));

    container.append(h('div', {class: 'actions-view'},
      h('section', {}, h('div', {class: 'toolbar'}, dom.filter), h('table', {}, dom.servers)),
      h('section', {class: 'pad'}, dom.readOnly, dom.editorTitle, dom.editorType,
        h('p', {class: 'warning', id: 'physical-warning'}, 'Sending a goal makes the action server act. A server that controls a robot will move it. ',
          'Goals are sent only after you confirm them. ', dom.exitPolicy),
        h('nav', {class: 'subtabs', style: 'padding:0;background:none'}, dom.modeForm, dom.modeJson,
          h('span', {style: 'flex:1'}),
          h('button', {class: 'action', id: 'reset-defaults', type: 'button', style: 'margin:4px 0',
            onclick: () => { if (editor) { putValue(described.goal_default); notify('Default values restored. Nothing was sent.'); } }}, 'Reset to defaults')),
        dom.form, dom.json, dom.errors,
        h('h3', {}, 'Saved configurations (this browser only)'), configurations,
        h('div', {class: 'row'}, dom.send, dom.cancel, dom.sendReason), dom.notice),
      h('section', {class: 'pad'}, dom.offline, h('h3', {style: 'margin-top:0'}, 'Selected goal'), dom.monitor,
        h('h3', {}, 'History (goals sent since the GUI server started)'),
        h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Sent'), h('th', {}, 'Action'), h('th', {}, 'State'), h('th', {}, 'UUID'), h('th', {}, 'Goal'))), dom.history))),
      dom.dialog);

    shownGoal = shownHistory = '';
    setMode(mode);
    renderConfigurations();
    refreshSession();
    stops = [
      poll(async () => { servers = (await getJSON('/actions/servers')).actions; renderServers(); }, 2000),
      poll(refreshGoals, 500, () => { dom.offline.style.display = ''; }),
      poll(refreshSession, 5000),
    ];
    if (selected) { const again = selected; selected = null; editor = null; selectAction(again.name, again.type); }
  },

  /** Stop polling and keep the draft of the action being edited; active goals are unaffected. */
  unmount() {
    rememberDraft();
    for (const stop of stops) stop();
    stops = [];
    if (dom.dialog && dom.dialog.open) dom.dialog.close();
    editor = null;
  },
};
