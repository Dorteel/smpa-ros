/**
 * The application shell: primary tabs and the gateway status indicator.
 *
 * Concepts: a tab module is an object with an id, a title, and mount and
 * unmount functions. The shell shows exactly one tab at a time. Unmounting a
 * tab must stop its polling and close its streams, so the gateway only does
 * work for what is visible. Adding a primary tab means adding one module to
 * the list below; nothing else in the shell changes.
 */
import {getJSON, h, poll} from './api.js';
import ros from './tabs/ros/index.js';
import sensors from './tabs/sensors/index.js';
import actions from './tabs/actions/index.js';
import smpa from './tabs/smpa/index.js';

const TABS = [ros, sensors, actions, smpa];
const workspace = document.getElementById('workspace');
const tabBar = document.getElementById('primary-tabs');
const connection = document.getElementById('connection');
let active = null;

/**
 * Show the tab named by the address fragment, or the first tab.
 *
 * The fragment looks like "#ros/topics": the part before the slash selects
 * the primary tab and the rest is handed to that tab as its own sub-route.
 * The previously shown tab is unmounted first so its streams are closed.
 */
function route() {
  const [tabId, ...rest] = location.hash.replace(/^#/, '').split('/');
  const tab = TABS.find((candidate) => candidate.id === tabId) || TABS[0];
  if (active !== tab) {
    if (active) active.unmount();
    workspace.replaceChildren();
    active = tab;
    tab.mount(workspace, rest.join('/'));
  } else if (tab.navigate) {
    tab.navigate(rest.join('/'));
  }
  for (const button of tabBar.children) {
    button.setAttribute('aria-selected', String(button.dataset.tab === tab.id));
  }
}

/**
 * Ask the gateway for its health and show the result in the top bar.
 *
 * A reachable gateway shows the ROS domain and middleware it joined. An
 * unreachable gateway is shown as offline; the views keep their last data
 * and recover by themselves when it returns.
 */
async function refreshHealth() {
  try {
    const health = await getJSON('/health');
    connection.textContent = `ROS ${health.ros.distro || ''} · domain ${health.ros.domain_id} · ${health.ros.rmw}`;
    connection.className = 'pill ' + (health.ros.ok ? 'pill-ok' : 'pill-warn');
  } catch (error) {
    connection.textContent = 'gateway offline';
    connection.className = 'pill pill-off';
  }
}

for (const tab of TABS) {
  tabBar.append(h('button', {'data-tab': tab.id, role: 'tab',
                             onclick: () => { location.hash = tab.id; }}, tab.title));
}
window.addEventListener('hashchange', route);
route();
poll(refreshHealth, 3000);
