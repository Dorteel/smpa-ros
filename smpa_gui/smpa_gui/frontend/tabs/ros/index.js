/**
 * The ROS 2 tab: generic inspection of whatever ROS graph is visible.
 *
 * Concepts: a subview is one inspector or manager (graph, workspace, topics,
 * parameters, services, actions) with the same mount and unmount contract as
 * a tab. Only one subview is mounted at a time, so only one of them polls
 * the gateway. The workspace arranges the discovered interfaces around an
 * empty canvas and opens the other subviews (and the Actions tab) to
 * inspect one of them.
 */
import {h} from '../../api.js';
import graph from './graph.js';
import workspace from './workspace.js';
import topics from './topics.js';
import parameters from './parameters.js';
import services from './services.js';
import actions from './actions.js';

const SUBVIEWS = [graph, workspace, topics, parameters, services, actions];
let bar = null;
let host = null;
let active = null;

/**
 * Show the subview named by the sub-route, defaulting to the graph.
 *
 * A sub-route may carry an argument after the subview id, for example
 * "topics//fixture/chatter" selects a topic; it is passed to the subview.
 */
function show(subRoute) {
  const [id, ...argument] = (subRoute || '').split('/');
  const subview = SUBVIEWS.find((candidate) => candidate.id === id) || SUBVIEWS[0];
  if (active) active.unmount();
  host.replaceChildren();
  active = subview;
  subview.mount(host, argument.join('/'));
  for (const button of bar.children) {
    button.setAttribute('aria-selected', String(button.dataset.view === subview.id));
  }
}

export default {
  id: 'ros',
  title: 'ROS 2',

  /** Build the subview bar and show the requested subview. */
  mount(container, subRoute) {
    bar = h('nav', {class: 'subtabs', 'aria-label': 'ROS 2 views'},
      SUBVIEWS.map((subview) => h('button', {'data-view': subview.id, role: 'tab',
        onclick: () => { location.hash = 'ros/' + subview.id; }}, subview.title)));
    host = h('div', {class: 'view'});
    container.append(bar, host);
    show(subRoute);
  },

  /** Switch subview when the address changes while this tab stays open. */
  navigate(subRoute) { show(subRoute); },

  /** Stop the mounted subview's polling and streams. */
  unmount() {
    if (active) active.unmount();
    active = null;
  },
};
