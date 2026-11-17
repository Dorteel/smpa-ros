/**
 * Services panel: discover services, inspect their request and response, and call one after confirmation.
 *
 * Concepts: a service is offered by provider nodes and used by client
 * nodes. Its request and response schemas come from the gateway, derived
 * from the installed interface definitions, so no service is known in
 * advance. The request is edited in a generated form or as JSON (the same
 * editor as goals in the Actions tab). A call is made only by the
 * confirmation button in a dialog that shows the complete request; each
 * confirmation carries a new request id, so a double click or a reload
 * cannot call twice. Calls are never retried. The history lists the calls
 * made through this GUI process since it started.
 */
import {getJSON, h, poll, postJSON} from '../../api.js';
import {createEditor, validate, withDefaults} from '../actions/form.js';

const KIND_LABEL = {parameter: 'parameter services', action: 'action plumbing', type_description: 'type description', other: ''};
const STATE_TEXT = {pending: 'waiting for the response…', succeeded: 'succeeded', timed_out: 'timed out (outcome unknown)',
  server_lost: 'provider disappeared (outcome unknown)', failed: 'failed'};
const filters = {text: '', parameter: false, action: false, type_description: false, hidden: false};
let services = [];
let selected = null;
let described = null;
let editor = null;
let mode = 'form';
let session = {enabled: false, token: null};
let calls = [];
let shownCall = null;
let calling = false;
let dom = {};
let stops = [];

/** Tell whether a service passes the current filters: text, kinds and hidden names. */
function visible(service) {
  if (service.kind !== 'other' && !filters[service.kind]) return false;
  if (service.hidden && !filters.hidden && service.kind === 'other') return false;
  const text = filters.text.trim().toLowerCase();
  return !text || service.name.toLowerCase().includes(text) || service.types.join(' ').toLowerCase().includes(text);
}

/** Redraw the service list with names, types and provider nodes, keeping the selection highlighted. */
function renderServices() {
  const shown = services.filter(visible);
  dom.count.textContent = `${shown.length} of ${services.length} services`;
  dom.list.replaceChildren(...(shown.length ? shown.map((service) => h('tr', {
    class: 'selectable' + (selected && service.name === selected.name ? ' selected' : ''), 'data-service': service.name,
    onclick: () => { location.hash = 'ros/services/' + service.name; }},
  h('td', {}, h('div', {class: 'mono'}, service.name), h('div', {class: 'muted', style: 'font-size:12px'},
    service.types.join(', '), KIND_LABEL[service.kind] ? ` · ${KIND_LABEL[service.kind]}` : ''),
  h('div', {class: 'muted', style: 'font-size:12px'}, service.providers.length ? 'provided by ' + service.providers.join(', ') : 'no provider'))))
    : [h('tr', {}, h('td', {class: 'empty'}, services.length ? 'No service matches the filters.' : 'No services discovered.'))]));
  if (selected) {
    const now = services.find((service) => service.name === selected.name);
    dom.providers.textContent = now ? (now.providers.join(', ') || 'none right now') : 'the service is no longer discovered';
    dom.clients.textContent = now ? (now.clients.join(', ') || 'none') : '–';
  }
}

/** Show a list of field schemas as an indented outline with names and types, for the response definition. */
function outline(schema, depth = 0) {
  if (!schema.fields.length) return [h('div', {class: 'muted', style: `padding-left:${depth * 14}px`}, '(no fields)')];
  return schema.fields.flatMap((field) => {
    const kind = field.schema.kind === 'array' ? field.schema.item : field.schema;
    const type = field.schema.kind === 'array' ? `${kind.type || kind.kind}[]` : (field.schema.type || field.schema.kind);
    const line = h('div', {style: `padding-left:${depth * 14}px`}, h('span', {class: 'mono'}, field.name), h('span', {class: 'muted'}, '  ' + type));
    return kind.kind === 'message' ? [line, ...outline(kind, depth + 1)] : [line];
  });
}

/** Show field problems below the editor and mark the inputs they belong to. */
function showErrors(errors) {
  dom.errors.replaceChildren(...errors.map((error) => h('li', {}, `${error.path || 'request'}: ${error.message}`)));
  if (editor) editor.showErrors(errors);
}

/** Show a short message about what happened, in the error style when needed. */
function notify(text, kind = 'note') {
  dom.notice.textContent = text;
  dom.notice.className = kind === 'error' ? 'error-text' : 'muted';
}

/**
 * Select a service and load its interface into a fresh editor.
 *
 * The request editor starts with the interface's default values. Nothing is
 * called. A type that is not available in the GUI's environment is
 * explained instead of an editor.
 */
async function selectService(name) {
  selected = services.find((service) => service.name === name) || (name ? {name, types: [], providers: [], clients: [], kind: 'other'} : null);
  described = null;
  editor = null;
  showErrors([]);
  notify('');
  renderServices();
  dom.title.textContent = selected ? selected.name : 'Select a service';
  dom.type.textContent = selected ? selected.types.join(', ') : '';
  dom.form.replaceChildren();
  dom.response.replaceChildren();
  dom.call.disabled = true;
  if (!selected) return;
  if (!selected.types.length) { dom.form.append(h('p', {class: 'muted'}, 'Waiting for the service to be discovered…')); return; }
  try {
    described = await getJSON('/ros/services/interface', {type: selected.types[0]});
  } catch (error) {
    dom.form.append(h('p', {class: 'notice', id: 'service-type-missing'}, error.message));
    return;
  }
  if (!selected || selected.types[0] !== described.type) return;
  editor = createEditor(described.request, described.request_default, () => showErrors([]));
  dom.form.append(editor.element);
  dom.json.value = JSON.stringify(described.request_default, null, 2);
  dom.responseOutline.replaceChildren(...outline(described.response));
  setMode(mode);
  updateControls();
}

/** Enable the call button only when changes are allowed, a request editor exists, and no call is running. */
function updateControls() {
  dom.call.disabled = !session.enabled || !editor || calling;
  dom.readOnly.style.display = session.enabled ? 'none' : '';
}

/** Read the request from the form or the JSON editor; invalid JSON gives an error entry instead of a value. */
function readValue() {
  if (mode === 'form') return {value: editor.get(), errors: []};
  try {
    return {value: JSON.parse(dom.json.value), errors: []};
  } catch (error) {
    return {value: undefined, errors: [{path: '', message: 'is not valid JSON: ' + error.message}]};
  }
}

/** Switch between the generated form and the JSON editor, carrying the value across; refuse invalid JSON. */
function setMode(next) {
  if (editor && next !== mode) {
    const current = readValue();
    if (current.value === undefined || (next === 'form' && (current.value === null || typeof current.value !== 'object' || Array.isArray(current.value)))) {
      showErrors(current.errors.length ? current.errors : [{path: '', message: 'must be a JSON object to be shown as a form'}]);
      return;
    }
    if (next === 'form') editor.set(withDefaults(described.request, current.value, described.request_default));
    else dom.json.value = JSON.stringify(current.value, null, 2);
    showErrors([]);
  }
  mode = next;
  dom.form.style.display = mode === 'form' ? '' : 'none';
  dom.json.style.display = mode === 'json' ? '' : 'none';
  dom.modeForm.setAttribute('aria-selected', String(mode === 'form'));
  dom.modeJson.setAttribute('aria-selected', String(mode === 'json'));
}

/**
 * Check the request and, if it fits the service type, open the confirmation dialog with the complete request.
 */
function askToCall() {
  if (!editor || !described || calling) return;
  const current = readValue();
  const errors = current.errors.length ? current.errors : validate(described.request, current.value);
  showErrors(errors);
  if (errors.length) { notify('The service was not called: fix the listed fields first.', 'error'); return; }
  const timeout = Number(dom.timeout.value);
  if (!(timeout >= 0.5 && timeout <= 60)) { notify('The timeout must be between 0.5 and 60 seconds.', 'error'); return; }
  notify('');
  const now = services.find((service) => service.name === selected.name);
  dom.dialogService.textContent = `${selected.name}  (${described.type})`;
  dom.dialogProviders.textContent = now && now.providers.length ? now.providers.join(', ') : 'none discovered right now';
  dom.dialogTimeout.textContent = `${timeout} s`;
  dom.dialogRequest.textContent = JSON.stringify(withDefaults(described.request, current.value, described.request_default), null, 2);
  dom.confirm.disabled = false;
  dom.dialog.pending = {service: selected.name, type: described.type, request: current.value, timeout};
  dom.dialog.showModal();
}

/**
 * Call the service once with the confirmed request and show the outcome.
 *
 * A fresh request id is made for this confirmation; the buttons stay
 * disabled while the call runs. The call is not retried. A network failure
 * means the outcome is unknown, and the user is told to check the history.
 */
async function callConfirmed() {
  const pending = dom.dialog.pending;
  dom.dialog.pending = null;
  dom.confirm.disabled = true;
  dom.dialog.close();
  if (!pending || calling) return;
  calling = true;
  updateControls();
  notify(`Calling ${pending.service}…`);
  const requestId = 'svc' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/-/g, '');
  let answer = null;
  try {
    answer = await postJSON('/ros/services/call', {...pending, request_id: requestId}, session.token);
  } catch (error) {
    notify('The request did not complete. It is not known whether the service was called: check the history before calling again.', 'error');
  }
  calling = false;
  updateControls();
  if (answer && answer.body.call) {
    shownCall = answer.body.call;
    notify(`Call ${STATE_TEXT[shownCall.state] || shownCall.state}.`, shownCall.state === 'succeeded' ? 'note' : 'error');
  } else if (answer) {
    showErrors(answer.body.errors || []);
    notify(`The service was not called: ${typeof answer.body.detail === 'string' ? answer.body.detail : 'the gateway refused the request'}`, 'error');
    if (answer.status === 403) refreshSession();
  }
  await refreshCalls();
}

/** Show one call record: its state, timing, request and response or error. */
function renderCall() {
  if (!shownCall) { dom.response.replaceChildren(h('p', {class: 'muted'}, 'No call selected.')); return; }
  const call = shownCall;
  const took = call.finished_at ? `${((call.finished_at - call.created_at) * 1000).toFixed(0)} ms` : '';
  dom.response.replaceChildren(
    h('div', {id: 'service-call', 'data-state': call.state},
      h('dl', {class: 'readout', style: 'padding:0'},
        h('dt', {}, 'Service'), h('dd', {}, call.service), h('dt', {}, 'State'), h('dd', {id: 'service-call-state'}, STATE_TEXT[call.state] || call.state),
        h('dt', {}, 'Sent'), h('dd', {}, new Date(call.created_at * 1000).toLocaleTimeString([], {hour12: false})),
        h('dt', {}, 'Took'), h('dd', {}, took)),
      call.error ? h('p', {class: 'warning', id: 'service-call-error'}, call.error) : null,
      h('h3', {}, 'Response'), h('pre', {id: 'service-response'}, call.response ? JSON.stringify(call.response, null, 2) : '–'),
      h('details', {}, h('summary', {class: 'muted'}, 'Request'), h('pre', {}, JSON.stringify(call.request, null, 2))),
      h('button', {class: 'action', type: 'button', id: 'service-reuse', onclick: () => reuse(call)}, 'Load this request into the editor')));
}

/** Put an earlier request into the editor of its service; this never calls anything. */
async function reuse(call) {
  if (!selected || selected.name !== call.service) {
    location.hash = 'ros/services/' + call.service;
    return;
  }
  if (!editor) return;
  editor.set(withDefaults(described.request, call.request, described.request_default));
  dom.json.value = JSON.stringify(call.request, null, 2);
  notify('Request loaded into the editor. Nothing was called.');
}

/** Redraw the history of calls made through this GUI process. */
function renderHistory() {
  dom.history.replaceChildren(...(calls.length ? calls.map((call) => h('tr', {class: 'selectable' + (shownCall && call.call_id === shownCall.call_id ? ' selected' : ''),
    'data-call': call.call_id, onclick: () => { shownCall = call; renderCall(); renderHistory(); }},
  h('td', {}, new Date(call.created_at * 1000).toLocaleTimeString([], {hour12: false})), h('td', {class: 'mono'}, call.service),
  h('td', {}, STATE_TEXT[call.state] || call.state))) : [h('tr', {}, h('td', {class: 'empty', colspan: 3}, 'No calls yet.'))]));
}

/** Fetch the call history and refresh the shown call from it. */
async function refreshCalls() {
  calls = (await getJSON('/ros/services/calls')).calls;
  if (shownCall) shownCall = calls.find((call) => call.call_id === shownCall.call_id) || shownCall;
  renderHistory();
  renderCall();
}

/** Ask the gateway whether changes are allowed and fetch the session token. */
async function refreshSession() {
  try {
    session = await getJSON('/session');
  } catch (error) {
    session = {enabled: false, token: null};
  }
  updateControls();
}

/** Build one labelled checkbox bound to a list filter. */
function toggle(key, label) {
  return h('label', {}, h('input', {type: 'checkbox', checked: filters[key], 'data-filter': key,
    onchange: (event) => { filters[key] = event.target.checked; renderServices(); }}), label);
}

export default {
  id: 'services',
  title: 'Services',

  /** Build the list, the editor, the response area and the dialog; start refreshing services and history. */
  async mount(container, argument) {
    dom = {
      list: h('tbody', {id: 'service-list'}), count: h('span', {class: 'count', id: 'service-count'}),
      title: h('h2', {id: 'service-title'}, 'Select a service'), type: h('div', {class: 'mono muted', id: 'service-type'}),
      providers: h('dd', {id: 'service-providers'}), clients: h('dd', {id: 'service-clients'}),
      form: h('div', {id: 'service-form'}),
      json: h('textarea', {id: 'service-json', spellcheck: 'false', style: 'display:none'}),
      errors: h('ul', {class: 'error-list', id: 'service-errors'}), notice: h('p', {class: 'muted', id: 'service-notice'}),
      timeout: h('input', {type: 'number', id: 'service-timeout', value: '10', min: '0.5', max: '60', step: '0.5', style: 'width:80px'}),
      call: h('button', {class: 'primary', id: 'service-call-button', type: 'button', disabled: true, onclick: askToCall}, 'Call service…'),
      readOnly: h('p', {class: 'warning', id: 'service-readonly', style: 'display:none'},
        'Service calls are disabled (read-only GUI or not on this computer). Services can be inspected but not called.'),
      responseOutline: h('div', {id: 'service-response-outline', style: 'font-size:12.5px'}),
      response: h('div', {id: 'service-result'}), history: h('tbody', {id: 'service-history'}),
      dialogService: h('dd', {class: 'mono'}), dialogProviders: h('dd', {class: 'mono'}), dialogTimeout: h('dd', {}),
      dialogRequest: h('pre', {id: 'service-confirm-request'}),
      confirm: h('button', {class: 'primary', id: 'service-confirm', type: 'button', onclick: callConfirmed}, 'Call now'),
    };
    dom.modeForm = h('button', {role: 'tab', type: 'button', onclick: () => setMode('form')}, 'Form');
    dom.modeJson = h('button', {role: 'tab', type: 'button', id: 'service-mode-json', onclick: () => setMode('json')}, 'JSON');
    dom.dialog = h('dialog', {id: 'service-dialog'},
      h('h2', {}, 'Call this service?'),
      h('p', {class: 'warning'}, 'The provider acts on this request as soon as it arrives. If it controls a robot or other hardware, ',
        'it may move or change state. A request that fits the service type is not necessarily safe. The call is made once and never repeated.'),
      h('dl', {class: 'readout', style: 'padding:0'}, h('dt', {}, 'Service'), dom.dialogService, h('dt', {}, 'Provider'), dom.dialogProviders,
        h('dt', {}, 'Timeout'), dom.dialogTimeout),
      dom.dialogRequest,
      h('div', {class: 'row'}, dom.confirm,
        h('button', {class: 'action', id: 'service-decline', type: 'button', onclick: () => { dom.dialog.pending = null; dom.dialog.close(); }}, 'Do not call')));
    dom.dialog.addEventListener('cancel', () => { dom.dialog.pending = null; });
    container.append(h('div', {class: 'actions-view'},
      h('section', {}, h('div', {class: 'toolbar'},
        h('input', {type: 'search', id: 'service-filter', placeholder: 'Filter services…', value: filters.text, style: 'flex:1',
          oninput: (event) => { filters.text = event.target.value; renderServices(); }}), dom.count),
      h('div', {class: 'toolbar'}, toggle('parameter', 'Parameter services'), toggle('action', 'Action plumbing'),
        toggle('type_description', 'Type description'), toggle('hidden', 'Hidden names')),
      h('table', {}, dom.list)),
      h('section', {class: 'pad'}, dom.readOnly, dom.title, dom.type,
        h('dl', {class: 'readout', style: 'padding:0'}, h('dt', {}, 'Providers'), dom.providers, h('dt', {}, 'Clients'), dom.clients),
        h('p', {class: 'warning', id: 'service-warning'}, 'Calling a service makes its provider act. A provider that controls a robot ',
          'may move it. Calls are made only after you confirm them, once, and are never retried.'),
        h('h3', {}, 'Request'),
        h('nav', {class: 'subtabs', style: 'padding:0;background:none'}, dom.modeForm, dom.modeJson, h('span', {style: 'flex:1'}),
          h('button', {class: 'action', id: 'service-reset', type: 'button', style: 'margin:4px 0', onclick: () => {
            if (editor) { editor.set(described.request_default); dom.json.value = JSON.stringify(described.request_default, null, 2); showErrors([]); notify('Default values restored. Nothing was called.'); }
          }}, 'Reset to defaults')),
        dom.form, dom.json, dom.errors,
        h('div', {class: 'row'}, dom.call, h('label', {}, 'Timeout (s)', dom.timeout)), dom.notice,
        h('h3', {}, 'Response definition'), dom.responseOutline),
      h('section', {class: 'pad'}, h('h3', {style: 'margin-top:0'}, 'Selected call'), dom.response,
        h('h3', {}, 'History (calls made since the GUI server started)'),
        h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Sent'), h('th', {}, 'Service'), h('th', {}, 'State'))), dom.history))),
    dom.dialog);
    setMode(mode);
    renderCall();
    await refreshSession();
    stops = [poll(async () => {
      services = (await getJSON('/ros/services')).services;
      renderServices();
      if (selected && !selected.types.length && services.some((service) => service.name === selected.name)) selectService(selected.name);
    }, 2500), poll(refreshCalls, 2000), poll(refreshSession, 5000)];
    selectService(argument ? (argument.startsWith('/') ? argument : '/' + argument) : null);
  },

  /** Stop polling and close the dialog; nothing that was called is affected. */
  unmount() {
    for (const stop of stops) stop();
    stops = [];
    if (dom.dialog && dom.dialog.open) dom.dialog.close();
    selected = null;
    editor = null;
  },
};
