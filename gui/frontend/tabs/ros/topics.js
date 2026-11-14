/**
 * Topics inspector: discovery, types, publishers, subscribers, QoS and live messages.
 *
 * Concepts: the list is refreshed by polling. Selecting a topic shows its
 * endpoints. Live messages are shown only after the user starts the echo;
 * the echo is a stream, so the gateway subscribes to the topic only while it
 * runs. Works for every message type, including types without a sensor viewer.
 */
import {getJSON, h, openStream, poll} from '../../api.js';

let stopList = null;
let closeEcho = null;
let selected = null;
let showHidden = false;
let filterText = '';
let listBody = null;
let detail = null;
let topics = [];

/**
 * Describe a QoS profile in one compact line.
 *
 * ROS discovery does not transmit the history setting or queue depth of
 * other nodes' endpoints, so those are shown only when they are known.
 */
function qosText(qos) {
  const history = qos.history === 'unknown' ? '' : `, ${qos.history}` + (qos.depth ? ` ${qos.depth}` : '');
  return `${qos.reliability}, ${qos.durability}${history}`;
}

/**
 * Redraw the topic list for the current filter text and hidden-name setting.
 *
 * The selected topic stays highlighted across refreshes. Each row shows the
 * name, the message type and the number of publishers and subscribers.
 */
function renderList() {
  const text = filterText.trim().toLowerCase();
  const rows = topics
    .filter((topic) => (showHidden || !topic.hidden) && (!text || topic.name.toLowerCase().includes(text)
      || topic.types.join(' ').toLowerCase().includes(text)))
    .map((topic) => h('tr', {class: 'selectable' + (topic.name === selected ? ' selected' : ''), 'data-topic': topic.name,
      onclick: () => { location.hash = 'ros/topics/' + topic.name; }},
      h('td', {class: 'mono'}, topic.name, h('div', {class: 'muted'}, topic.types.join(', '))),
      h('td', {class: 'count'}, `${topic.publisher_count}▸ ${topic.subscriber_count}◂`)));
  listBody.replaceChildren(...(rows.length ? rows : [h('tr', {}, h('td', {class: 'empty'}, 'No topics discovered.'))]));
}

/** Stop the running echo, if any, so the gateway unsubscribes. */
function stopEcho() {
  if (closeEcho) closeEcho();
  closeEcho = null;
}

/** Build a table of endpoints (publishers or subscribers) with their QoS. */
function endpointTable(endpoints) {
  if (endpoints.length === 0) return h('p', {class: 'muted'}, 'None.');
  return h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Node'), h('th', {}, 'Type'), h('th', {}, 'QoS'))),
    h('tbody', {}, endpoints.map((endpoint) => h('tr', {},
      h('td', {class: 'mono'}, endpoint.node), h('td', {class: 'mono'}, endpoint.type), h('td', {}, qosText(endpoint.qos))))));
}

/**
 * Show one topic: its types, endpoints and a start/stop control for live messages.
 *
 * Any echo of a previously selected topic is stopped first. If the topic is
 * not known to the gateway (it may have disappeared), say so instead of
 * failing. While the echo runs, each message replaces the previous one and
 * status messages explain silence.
 */
async function showTopic(name) {
  stopEcho();
  selected = name || null;
  renderList();
  detail.replaceChildren();
  if (!selected) { detail.append(h('p', {class: 'empty'}, 'Select a topic to inspect it.')); return; }
  let info;
  try {
    info = (await getJSON('/ros/topics/info', {name: selected})).topic;
  } catch (error) {
    detail.append(h('h2', {}, selected), h('p', {class: 'notice'}, error.message));
    return;
  }
  if (name !== selected) return;
  const output = h('pre', {id: 'echo-output'}, 'Echo is stopped.');
  const state = h('span', {class: 'muted', id: 'echo-state'}, '');
  const button = h('button', {class: 'action', id: 'echo-toggle'}, 'Start echo');
  button.onclick = () => {
    if (closeEcho) { stopEcho(); button.textContent = 'Start echo'; state.textContent = 'stopped'; return; }
    button.textContent = 'Stop echo';
    state.textContent = 'connecting…';
    closeEcho = openStream('/ros/topics/echo', {name: selected, type: info.types[0]}, {
      onData: (message) => {
        output.textContent = JSON.stringify(message.message, null, 2);
        state.textContent = `${message.rate_hz} Hz received` + (message.truncated ? ' · long fields shortened' : '');
      },
      onStatus: (status) => { state.textContent = `${status.state}: ${status.detail || ''}`; },
    });
  };
  detail.append(h('h2', {}, info.name), h('p', {class: 'mono muted'}, info.types.join(', ')),
    h('h3', {}, `Publishers (${info.publishers.length})`), endpointTable(info.publishers),
    h('h3', {}, `Subscribers (${info.subscribers.length})`), endpointTable(info.subscribers),
    h('h3', {}, 'Live messages'), h('div', {class: 'toolbar', style: 'border:0;padding:0;background:none'}, button, state), output);
}

export default {
  id: 'topics',
  title: 'Topics',

  /** Build the list and detail areas and start refreshing the topic list. */
  mount(container, argument) {
    listBody = h('tbody', {id: 'topic-list'});
    detail = h('section', {class: 'detail', id: 'topic-detail'});
    container.append(h('div', {class: 'sidebar column'},
      h('div', {class: 'toolbar'},
        h('input', {type: 'search', placeholder: 'Filter topics…', value: filterText, style: 'flex:1',
                    oninput: (event) => { filterText = event.target.value; renderList(); }}),
        h('label', {}, h('input', {type: 'checkbox', checked: showHidden,
          onchange: (event) => { showHidden = event.target.checked; renderList(); }}), 'Hidden')),
      h('table', {}, listBody)), detail);
    stopList = poll(async () => { topics = (await getJSON('/ros/topics')).topics; renderList(); }, 2000);
    showTopic(argument ? (argument.startsWith('/') ? argument : '/' + argument) : null);
  },

  /** Stop the list refresh and any running echo. */
  unmount() {
    if (stopList) stopList();
    stopEcho();
    stopList = null;
  },
};
