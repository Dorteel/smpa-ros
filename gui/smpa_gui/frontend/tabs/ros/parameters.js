/**
 * Parameters panel: read a node's parameters, propose new values, and apply them only after confirmation.
 *
 * Concepts: reading parameters asks the node itself, so a busy or stopped
 * node may not answer; that is shown as a state of the node. A proposal is
 * a new value (and, with dynamic typing, a new type) for one writable
 * parameter. Proposals are checked as they are typed, listed beside the
 * current values, and sent together only when the user confirms them in a
 * dialog. The gateway checks them again and the node decides; its answer is
 * shown per parameter and the values are read back afterwards. Nothing is
 * ever changed by selecting, typing, or reloading.
 */
import {getJSON, h, poll, postJSON} from '../../api.js';
import {SETTABLE_TYPES, check, constraintText, displayValue, editText, parseInput} from './parameter_values.js';

const STATE_TEXT = {
  not_responding: 'This node did not answer in time. It may be busy (for example running inference) or not spinning. Try again later.',
  unavailable: 'This node does not offer parameter services.',
};
let stopList = null;
let nodes = [];
let selected = null;
let listBody = null;
let detail = null;
let filterText = '';
let parameterFilter = '';
let parameters = [];
let proposals = new Map();
let session = {enabled: false, token: null};
let dom = {};
let applying = false;
let lastOutcome = null;
let focusedParameter = null;

/** Redraw the node list, keeping the selected node highlighted. */
function renderList() {
  const text = filterText.trim().toLowerCase();
  const rows = nodes.filter((node) => !node.hidden && (!text || node.full_name.toLowerCase().includes(text)))
    .map((node) => h('tr', {class: 'selectable' + (node.full_name === selected ? ' selected' : ''), 'data-node': node.full_name,
      onclick: () => { location.hash = 'ros/parameters/' + node.full_name; }},
      h('td', {class: 'mono'}, node.full_name, node.is_gateway ? h('span', {class: 'muted'}, '  (this GUI)') : null)));
  listBody.replaceChildren(...(rows.length ? rows : [h('tr', {}, h('td', {class: 'empty'}, 'No nodes discovered.'))]));
}

/** Show a parameter value compactly; arrays and strings are shown as JSON. */
function valueText(value) {
  return value === null || value === undefined ? '–' : (typeof value === 'object' || typeof value === 'string'
    ? JSON.stringify(value) : String(value));
}

/** Tell whether a parameter may be edited here: changes enabled, known type, and not read-only. */
function editable(parameter) {
  return session.enabled && !parameter.read_only && parameter.type !== 'not_set';
}

/**
 * Start, update or drop the proposal for one parameter from the text and type the user chose.
 *
 * The text is parsed for the chosen type and the result is checked against
 * the parameter's descriptor. The proposal keeps the text, so an invalid
 * entry is shown as typed together with its problems.
 */
function propose(parameter, type, text) {
  const parsed = parseInput(type, text);
  const problems = parsed.error ? [parsed.error] : check(parameter, type, parsed.value);
  proposals.set(parameter.name, {name: parameter.name, type, text, value: parsed.value, problems});
  renderPending();
  markRow(parameter.name);
}

/** Mark one row's input as valid or invalid and show its problems next to it. */
function markRow(name) {
  const proposal = proposals.get(name);
  const row = dom.table && dom.table.querySelector(`tr[data-parameter="${CSS.escape(name)}"]`);
  if (!row || !proposal) return;
  const input = row.querySelector('.proposal-input');
  if (input) { input.classList.toggle('invalid', proposal.problems.length > 0); input.title = proposal.problems.join('; '); }
  const note = row.querySelector('.proposal-note');
  if (note) note.textContent = proposal.problems.join('; ');
}

/** Build the editing controls for a parameter that has a proposal: type choice, value input and remove button. */
function proposalEditor(parameter) {
  const proposal = proposals.get(parameter.name);
  const types = parameter.dynamic_typing ? SETTABLE_TYPES : [parameter.type];
  const typeSelect = h('select', {class: 'proposal-type', disabled: types.length === 1, 'aria-label': `Type of ${parameter.name}`,
    onchange: (event) => { propose(parameter, event.target.value, proposal.text); renderTable(); }},
    types.map((type) => h('option', {value: type}, type)));
  typeSelect.value = proposal.type;
  const input = proposal.type === 'bool'
    ? h('select', {class: 'proposal-input', 'aria-label': `New value of ${parameter.name}`,
      onchange: (event) => propose(parameter, 'bool', event.target.value)},
      h('option', {value: 'true'}, 'true'), h('option', {value: 'false'}, 'false'))
    : h('input', {type: 'text', class: 'proposal-input mono', value: proposal.text, 'aria-label': `New value of ${parameter.name}`,
      placeholder: proposal.type.endsWith('_array') ? 'JSON list, e.g. [1, 2]' : '',
      oninput: (event) => propose(parameter, proposal.type, event.target.value)});
  if (proposal.type === 'bool') input.value = proposal.text === 'false' ? 'false' : 'true';
  return h('div', {class: 'proposal'},
    h('div', {class: 'row', style: 'margin:0'}, typeSelect, input,
      h('button', {class: 'action', type: 'button', title: 'Discard this proposal', 'data-discard': parameter.name,
        onclick: () => { proposals.delete(parameter.name); renderTable(); renderPending(); }}, '×')),
    h('div', {class: 'proposal-note error-text'}));
}

/** Draw the parameter table with current values, constraints and, where proposed, the editing controls. */
function renderTable() {
  const text = parameterFilter.trim().toLowerCase();
  const shown = parameters.filter((parameter) => !text || parameter.name.toLowerCase().includes(text));
  dom.table.replaceChildren(h('thead', {}, h('tr', {}, h('th', {}, 'Name'), h('th', {}, 'Type'), h('th', {}, 'Current value'),
    h('th', {}, 'Constraints'), h('th', {}, 'Description'), h('th', {}, 'Proposed value'))),
    h('tbody', {}, shown.map((parameter) => h('tr', {'data-parameter': parameter.name,
      class: parameter.name === focusedParameter ? 'selected' : null, 'data-focused': parameter.name === focusedParameter},
      h('td', {class: 'mono'}, parameter.name), h('td', {}, parameter.type),
      h('td', {class: 'mono'}, displayValue(parameter.type, parameter.value)),
      h('td', {class: 'muted'}, constraintText(parameter)), h('td', {}, parameter.description || ''),
      h('td', {}, proposals.has(parameter.name) ? proposalEditor(parameter)
        : editable(parameter) ? h('button', {class: 'action', type: 'button', 'data-edit': parameter.name,
          onclick: () => { propose(parameter, parameter.type, editText(parameter.type, parameter.value)); renderTable(); }}, 'Edit')
          : h('span', {class: 'muted'}, parameter.read_only ? 'read-only' : session.enabled ? '' : 'changes disabled'))))));
  for (const name of proposals.keys()) markRow(name);
}

/**
 * Bring the parameter that was asked for into view, once, after the table is first drawn.
 *
 * The ROS 2 Workspace opens this view for one parameter. That parameter's
 * row is highlighted by ``renderTable``; here it is scrolled into view so
 * the user sees it without searching. Nothing is proposed or changed: the
 * user still edits it with its Edit button and confirms in the dialog.
 */
function revealFocusedParameter() {
  const row = focusedParameter && dom.table && dom.table.querySelector(`tr[data-parameter="${CSS.escape(focusedParameter)}"]`);
  if (row) row.scrollIntoView({block: 'center'});
}

/**
 * Split the part of the address after "parameters/" into a node name and an optional parameter name.
 *
 * The address names a node, for example "fixture_mgmt/settings", and may
 * add "@" and one of its parameters, for example
 * "fixture_mgmt/settings@level". ROS node names cannot contain "@", so the
 * split is unambiguous. The node name gets its leading slash back.
 */
function nodeAndParameter(argument) {
  if (!argument) return {node: null, parameter: null};
  const [node, ...rest] = argument.split('@');
  return {node: node.startsWith('/') ? node : '/' + node, parameter: rest.length ? rest.join('@') : null};
}

/** Summarise the proposals and enable the apply button only when there are valid ones and nothing is being applied. */
function renderPending() {
  const list = [...proposals.values()];
  const invalid = list.filter((proposal) => proposal.problems.length).length;
  dom.pending.textContent = list.length
    ? `${list.length} proposed change${list.length === 1 ? '' : 's'}` + (invalid ? ` · ${invalid} with problems` : '') : 'No proposed changes.';
  dom.apply.disabled = applying || !session.enabled || !list.length || invalid > 0;
}

/** Find the parameter with a name in the last values read. */
function current(name) {
  return parameters.find((parameter) => parameter.name === name);
}

/**
 * Open the confirmation dialog listing every proposed change with its current and proposed value.
 *
 * Only valid proposals reach this point. The dialog states the node, the
 * atomic choice and the warning that a parameter change can make a robot
 * act differently; a type-valid value is not necessarily a safe one.
 */
function askToApply() {
  const list = [...proposals.values()];
  if (!list.length || list.some((proposal) => proposal.problems.length)) return;
  dom.dialogNode.textContent = selected;
  dom.dialogChanges.replaceChildren(h('thead', {}, h('tr', {}, h('th', {}, 'Parameter'), h('th', {}, 'Current'), h('th', {}, 'Proposed'))),
    h('tbody', {}, list.map((proposal) => {
      const parameter = current(proposal.name);
      return h('tr', {}, h('td', {class: 'mono'}, proposal.name),
        h('td', {class: 'mono'}, `${parameter ? displayValue(parameter.type, parameter.value) : '–'} (${parameter ? parameter.type : '?'})`),
        h('td', {class: 'mono'}, `${valueText(proposal.value)} (${proposal.type})`));
    })));
  dom.dialogAtomic.textContent = dom.atomic.checked
    ? 'All changes are applied together or not at all (atomic).'
    : 'Each change is applied on its own; some may succeed while others are rejected.';
  dom.confirm.disabled = false;
  dom.dialog.pending = {node: selected, atomic: dom.atomic.checked,
    changes: list.map((proposal) => ({name: proposal.name, type: proposal.type, value: proposal.value}))};
  dom.dialog.showModal();
}

/**
 * Send the confirmed changes exactly once and show the node's answer.
 *
 * A fresh request id is made for this confirmation and the buttons stay
 * disabled until the answer arrives, so a double click cannot apply twice.
 * The request is never retried. Successful proposals are removed; rejected
 * ones stay so they can be corrected. If the network fails the outcome is
 * unknown, and the values are read again before anything else is offered.
 */
async function applyConfirmed() {
  const pending = dom.dialog.pending;
  dom.dialog.pending = null;
  dom.confirm.disabled = true;
  dom.dialog.close();
  if (!pending || applying) return;
  applying = true;
  renderPending();
  const requestId = 'param' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/-/g, '');
  let answer = null;
  try {
    answer = await postJSON('/ros/parameters/set', {...pending, request_id: requestId}, session.token);
  } catch (error) {
    lastOutcome = {kind: 'error', text: 'The request did not complete. It is not known whether the parameters changed; the values are read again now.'};
  }
  applying = false;
  if (answer && answer.ok) {
    const record = answer.body.change;
    for (const result of record.results) if (result.successful) proposals.delete(result.name);
    parameters = record.parameters.length ? record.parameters : parameters;
    lastOutcome = {kind: record.outcome === 'applied' ? 'ok' : 'error', record};
  } else if (answer) {
    lastOutcome = {kind: 'error', text: `Nothing was changed: ${typeof answer.body.detail === 'string' ? answer.body.detail : 'the gateway refused the request'}`,
      errors: answer.body.errors || []};
    if (answer.status === 403) refreshSession();
  }
  if (!answer || !answer.ok) await showNode(selected, true);
  else { renderTable(); renderPending(); renderOutcome(); }
}

/** Show the outcome of the last apply: the node's answer per parameter, or why nothing was sent. */
function renderOutcome() {
  if (!dom.outcome) return;
  if (!lastOutcome) { dom.outcome.replaceChildren(); return; }
  const text = {applied: 'All changes were applied.', rejected: 'The node rejected the changes; nothing was applied.',
    partial: 'Some changes were applied and some were rejected.', unknown: 'The node did not answer in time: it is unknown whether the changes were applied.',
    unavailable: 'The node does not offer the set service; nothing was applied.'};
  const record = lastOutcome.record;
  dom.outcome.replaceChildren(h('div', {class: lastOutcome.kind === 'ok' ? 'success' : 'warning', id: 'parameter-outcome',
    'data-outcome': record ? record.outcome : 'refused'},
    h('strong', {}, record ? text[record.outcome] : lastOutcome.text),
    record ? h('ul', {}, record.results.map((result) => h('li', {'data-result': result.name},
      h('span', {class: 'mono'}, result.name), `: ${displayValue(result.type, result.previous_value)} → ${displayValue(result.type, result.value)} `,
      result.successful === true ? '✓ applied' : result.successful === false ? `✗ rejected${result.reason ? ': ' + result.reason : ''}` : `? ${result.reason}`))) : null,
    lastOutcome.errors && lastOutcome.errors.length ? h('ul', {class: 'error-list'},
      lastOutcome.errors.map((error) => h('li', {}, `${error.path}: ${error.message}`))) : null));
}

/**
 * Read and show the parameters of one node.
 *
 * While the request is pending the panel says so. A node that does not
 * answer or has no parameter services gets an explanation and a retry
 * button. If the user selected another node meanwhile, the late answer is
 * discarded. Proposals belong to one node and are dropped when another node
 * is selected; a refresh of the same node keeps them.
 */
async function showNode(name, keepOutcome = false) {
  if (name !== selected) { proposals = new Map(); lastOutcome = null; }
  else if (!keepOutcome) lastOutcome = null;
  selected = name || null;
  renderList();
  detail.replaceChildren();
  if (!selected) { detail.append(h('p', {class: 'empty'}, 'Select a node to read its parameters.')); return; }
  const retry = h('button', {class: 'action', id: 'parameter-refresh', onclick: () => showNode(name)}, 'Refresh');
  detail.append(h('h2', {}, selected), h('p', {class: 'muted', id: 'parameter-state'}, 'Reading parameters…'));
  let result;
  try {
    result = await getJSON('/ros/nodes/parameters', {node: selected});
  } catch (error) {
    result = {state: 'error', parameters: [], detail: error.message};
  }
  if (name !== selected) return;
  detail.replaceChildren(h('h2', {}, selected));
  if (result.state !== 'ok') {
    parameters = [];
    detail.append(h('p', {class: 'notice', id: 'parameter-state', 'data-state': result.state},
      STATE_TEXT[result.state] || result.detail || 'Parameters could not be read.'), retry);
    dom.outcome = h('div', {id: 'parameter-outcome-area'});
    detail.append(dom.outcome);
    renderOutcome();
    return;
  }
  parameters = result.parameters;
  dom.table = h('table', {id: 'parameter-table'});
  dom.pending = h('span', {class: 'muted', id: 'parameter-pending'});
  dom.atomic = h('input', {type: 'checkbox', id: 'parameter-atomic', checked: true});
  dom.apply = h('button', {class: 'primary', id: 'parameter-apply', type: 'button', onclick: askToApply}, 'Apply changes…');
  dom.outcome = h('div', {id: 'parameter-outcome-area'});
  detail.append(
    h('div', {class: 'row'}, h('span', {class: 'muted', id: 'parameter-state', 'data-state': 'ok'}, `${result.parameters.length} parameters`),
      retry, h('input', {type: 'search', id: 'parameter-filter', placeholder: 'Filter parameters…', value: parameterFilter,
        oninput: (event) => { parameterFilter = event.target.value; renderTable(); }})),
    session.enabled
      ? h('p', {class: 'warning', id: 'parameter-warning'}, 'Changing a parameter can change what a running node does immediately, ',
        'including how a robot moves. A value that fits the parameter\'s type and range is not necessarily safe. ',
        'Changes are applied only after you confirm them.')
      : h('p', {class: 'muted', id: 'parameter-readonly'}, 'Parameter changes are disabled (read-only GUI or not on this computer); values can be read.'),
    dom.table,
    h('div', {class: 'row'}, dom.pending, h('label', {}, dom.atomic, 'Apply atomically (all or nothing)'), dom.apply),
    dom.outcome);
  renderTable();
  renderPending();
  renderOutcome();
  revealFocusedParameter();
}

/** Ask the gateway whether changes are allowed and fetch the current session token. */
async function refreshSession() {
  try {
    session = await getJSON('/session');
  } catch (error) {
    session = {enabled: false, token: null};
  }
}

export default {
  id: 'parameters',
  title: 'Parameters',

  /** Build the node list, the detail area and the confirmation dialog, then start refreshing the node list. */
  async mount(container, argument) {
    listBody = h('tbody', {id: 'node-list'});
    detail = h('section', {class: 'detail', id: 'parameter-detail'});
    dom = {dialogNode: h('dd', {class: 'mono', id: 'parameter-confirm-node'}), dialogChanges: h('table', {id: 'parameter-confirm-changes'}),
      dialogAtomic: h('p', {class: 'muted'}),
      confirm: h('button', {class: 'primary', id: 'parameter-confirm', type: 'button', onclick: applyConfirmed}, 'Apply now')};
    dom.dialog = h('dialog', {id: 'parameter-dialog'},
      h('h2', {}, 'Apply these parameter changes?'),
      h('p', {class: 'warning'}, 'The node starts using the new values immediately. If it controls a robot or other hardware, its ',
        'behaviour can change at once. Make sure that is safe.'),
      h('dl', {class: 'readout', style: 'padding:0'}, h('dt', {}, 'Node'), dom.dialogNode), dom.dialogChanges, dom.dialogAtomic,
      h('div', {class: 'row'}, dom.confirm,
        h('button', {class: 'action', id: 'parameter-decline', type: 'button', onclick: () => { dom.dialog.pending = null; dom.dialog.close(); }}, 'Do not apply')));
    dom.dialog.addEventListener('cancel', () => { dom.dialog.pending = null; });
    container.append(h('div', {class: 'sidebar column'},
      h('div', {class: 'toolbar'}, h('input', {type: 'search', placeholder: 'Filter nodes…', value: filterText, style: 'flex:1',
        oninput: (event) => { filterText = event.target.value; renderList(); }})),
      h('table', {}, listBody)), detail, dom.dialog);
    stopList = poll(async () => { nodes = (await getJSON('/ros/nodes')).nodes; renderList(); }, 2000);
    await refreshSession();
    const requested = nodeAndParameter(argument);
    focusedParameter = requested.parameter;
    showNode(requested.node);
  },

  /** Stop refreshing the node list and close the dialog; proposals are discarded with the view. */
  unmount() {
    if (stopList) stopList();
    stopList = null;
    if (dom.dialog && dom.dialog.open) dom.dialog.close();
    selected = null;
    focusedParameter = null;
    proposals = new Map();
  },
};
