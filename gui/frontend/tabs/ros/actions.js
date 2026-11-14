/**
 * Actions inspector: discovered actions with their types, servers and clients.
 *
 * Concepts: an action server is a node that can execute goals of an action;
 * an action client is a node that can send them. This view only lists them.
 * It never sends, cancels or monitors goals.
 */
import {getJSON, h, poll} from '../../api.js';

let stop = null;

/** List node names one per line, or a dash when there are none. */
function nodeList(names) {
  return names.length ? names.map((name) => h('div', {class: 'mono'}, name)) : h('span', {class: 'muted'}, '–');
}

export default {
  id: 'actions',
  title: 'Actions',

  /**
   * Build the table and refresh it every two seconds.
   *
   * An empty ROS graph shows an explanatory row instead of an empty table.
   */
  mount(container) {
    const body = h('tbody', {id: 'action-list'});
    const count = h('span', {class: 'count'}, '');
    container.append(h('div', {class: 'column', style: 'flex:1'},
      h('div', {class: 'toolbar'}, h('span', {}, 'Discovered actions (read-only)'), count),
      h('div', {class: 'detail', style: 'padding:0'}, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'Action'), h('th', {}, 'Type'), h('th', {}, 'Servers'), h('th', {}, 'Clients'))), body))));
    stop = poll(async () => {
      const actions = (await getJSON('/ros/actions')).actions;
      count.textContent = `${actions.length} found`;
      body.replaceChildren(...(actions.length ? actions.map((action) => h('tr', {'data-action': action.name},
        h('td', {class: 'mono'}, action.name), h('td', {class: 'mono'}, action.types.join(', ')),
        h('td', {}, nodeList(action.servers)), h('td', {}, nodeList(action.clients))))
        : [h('tr', {}, h('td', {class: 'empty', colspan: 4}, 'No actions discovered.'))]));
    }, 2000);
  },

  /** Stop refreshing. */
  unmount() {
    if (stop) stop();
    stop = null;
  },
};
