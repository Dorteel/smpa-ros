/**
 * Parameters inspector: nodes and their parameter names, types, values and descriptions.
 *
 * Concepts: reading parameters asks the node itself, so a busy or stopped
 * node may not answer. That is shown as a state of the node, not as an
 * error of the GUI. Values are read once per selection or refresh and are
 * never written.
 */
import {getJSON, h, poll} from '../../api.js';

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

/**
 * Read and show the parameters of one node.
 *
 * While the request is pending the panel says so. A node that does not
 * answer or has no parameter services gets an explanation and a retry
 * button. If the user selected another node meanwhile, the late answer is
 * discarded.
 */
async function showNode(name) {
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
    detail.append(h('p', {class: 'notice', id: 'parameter-state', 'data-state': result.state},
      STATE_TEXT[result.state] || result.detail || 'Parameters could not be read.'), retry);
    return;
  }
  detail.append(h('p', {class: 'muted', id: 'parameter-state', 'data-state': 'ok'},
    `${result.parameters.length} parameters · read-only view `), retry,
    h('table', {id: 'parameter-table'}, h('thead', {}, h('tr', {}, h('th', {}, 'Name'), h('th', {}, 'Type'),
      h('th', {}, 'Value'), h('th', {}, 'Description'))),
      h('tbody', {}, result.parameters.map((parameter) => h('tr', {},
        h('td', {class: 'mono'}, parameter.name), h('td', {}, parameter.type),
        h('td', {class: 'mono'}, valueText(parameter.value)), h('td', {}, parameter.description || ''))))));
}

export default {
  id: 'parameters',
  title: 'Parameters',

  /** Build the node list and detail area and start refreshing the node list. */
  mount(container, argument) {
    listBody = h('tbody', {id: 'node-list'});
    detail = h('section', {class: 'detail', id: 'parameter-detail'});
    container.append(h('div', {class: 'sidebar column'},
      h('div', {class: 'toolbar'}, h('input', {type: 'search', placeholder: 'Filter nodes…', value: filterText, style: 'flex:1',
        oninput: (event) => { filterText = event.target.value; renderList(); }})),
      h('table', {}, listBody)), detail);
    stopList = poll(async () => { nodes = (await getJSON('/ros/nodes')).nodes; renderList(); }, 2000);
    showNode(argument ? (argument.startsWith('/') ? argument : '/' + argument) : null);
  },

  /** Stop refreshing the node list. */
  unmount() {
    if (stopList) stopList();
    stopList = null;
  },
};
