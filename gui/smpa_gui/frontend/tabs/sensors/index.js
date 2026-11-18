/**
 * The Sensors tab: live visualizations of discovered sensor topics in panels.
 *
 * Concepts: a panel shows one viewer connected to one topic. Several panels
 * can be open at once, including several of the same kind. A panel's stream
 * is open only while the panel exists and this tab is shown, so the gateway
 * subscribes to a sensor topic only while its visualization is active. The
 * list of open panels is remembered while the page is open, so switching to
 * another tab and back restores them.
 */
import {getJSON, h, openStream, poll} from '../../api.js';
import {VIEWERS} from './registry.js';

const remembered = [];
let nextPanelId = 1;
let mounted = [];
let discovered = [];
let listHost = null;
let panelHost = null;
let stopDiscovery = null;

/**
 * Connect a mounted panel to its selected topic, replacing any earlier connection.
 *
 * The previous stream is closed first, which makes the gateway unsubscribe
 * from the old topic, and the viewer is reset so stale data is not shown
 * under a new topic's name. With no topic selected the panel just waits.
 * Data goes to the viewer; a viewer that throws on unexpected data is
 * reported in the panel's status line instead of breaking the page.
 */
function connect(panel) {
  if (panel.close) panel.close();
  panel.close = null;
  panel.viewer.reset();
  const config = panel.config;
  if (!config.topic) { setStatus(panel, 'idle', 'Select a topic'); return; }
  setStatus(panel, 'connecting', config.topic);
  panel.close = openStream('/sensors/stream', {topic: config.topic, type: config.type || '',
    ...(VIEWERS[config.viewer].module.streamOptions || {})}, {
    onData: (header, binary) => {
      try {
        panel.viewer.update(header, binary);
        setStatus(panel, 'streaming', `${header.rate_hz} Hz` + (header.frame_id ? ` · frame ${header.frame_id}` : ''));
      } catch (error) {
        setStatus(panel, 'error', 'Viewer could not draw this message: ' + error.message);
      }
    },
    onStatus: (status) => setStatus(panel, status.state, status.detail || ''),
  });
}

/** Show a panel's stream state and a short explanation in its footer. */
function setStatus(panel, state, detail) {
  panel.state.textContent = state;
  panel.state.dataset.state = state;
  panel.note.textContent = detail;
}

/**
 * Fill a panel's topic selector with the discovered topics its viewer can show.
 *
 * The current selection is kept even when its topic is not currently
 * discovered; it is then marked as unavailable, so a sensor that restarts
 * reconnects without the user choosing it again.
 */
function fillTopics(panel) {
  const compatible = discovered.filter((item) => item.viewer === panel.config.viewer);
  const options = [h('option', {value: ''}, compatible.length ? '— select topic —' : '— no compatible topics —')];
  for (const item of compatible) {
    options.push(h('option', {value: item.topic, 'data-type': item.type},
      item.topic + (item.publisher_count ? '' : '  (no publisher)')));
  }
  if (panel.config.topic && !compatible.some((item) => item.topic === panel.config.topic)) {
    options.push(h('option', {value: panel.config.topic, 'data-type': panel.config.type || ''}, panel.config.topic + '  (unavailable)'));
  }
  panel.select.replaceChildren(...options);
  panel.select.value = panel.config.topic || '';
}

/**
 * Create the DOM and viewer for one remembered panel and connect it.
 *
 * The panel has a header with the viewer's name, a topic selector and a
 * close button; a body owned by the viewer; and a footer with the stream
 * state. Choosing another topic reconnects the same viewer.
 */
function mountPanel(config) {
  const entry = VIEWERS[config.viewer];
  const body = h('div', {class: 'panel-body'});
  const panel = {config, close: null, state: h('span', {class: 'panel-state'}, ''), note: h('span', {}, ''),
                 select: h('select', {'aria-label': 'Topic'})};
  panel.element = h('section', {class: 'panel', 'data-panel': config.id, 'data-viewer': config.viewer},
    h('div', {class: 'panel-head'}, h('strong', {}, entry.label), panel.select,
      h('button', {class: 'action', title: 'Close panel', onclick: () => removePanel(config.id)}, '✕')),
    body, h('div', {class: 'panel-foot'}, panel.state, panel.note));
  panelHost.append(panel.element);
  panel.viewer = entry.module.create(body);
  panel.select.onchange = () => {
    const option = panel.select.selectedOptions[0];
    config.topic = panel.select.value || null;
    config.type = option ? option.dataset.type || null : null;
    connect(panel);
  };
  fillTopics(panel);
  mounted.push(panel);
  connect(panel);
  renderHint();
}

/** Close a mounted panel's stream and release its viewer and DOM. */
function unmountPanel(panel) {
  if (panel.close) panel.close();
  panel.viewer.destroy();
  panel.element.remove();
}

/** Remember a new panel for a viewer and optional topic, and show it. */
function addPanel(viewer, topic = null, type = null) {
  const config = {id: nextPanelId++, viewer, topic, type};
  remembered.push(config);
  mountPanel(config);
}

/** Forget a panel and close it, which ends its subscription. */
function removePanel(id) {
  const index = remembered.findIndex((config) => config.id === id);
  if (index >= 0) remembered.splice(index, 1);
  const panel = mounted.find((candidate) => candidate.config.id === id);
  if (panel) unmountPanel(panel);
  mounted = mounted.filter((candidate) => candidate.config.id !== id);
  renderHint();
}

/** Show a hint in the panel area while no panel is open. */
function renderHint() {
  const hint = panelHost.querySelector('.empty');
  if (mounted.length === 0 && !hint) {
    panelHost.append(h('p', {class: 'empty', style: 'grid-column:1/-1'},
      'Open a discovered sensor topic from the list, or add an empty panel and choose a topic later.'));
  } else if (mounted.length > 0 && hint) {
    hint.remove();
  }
}

/**
 * Redraw the list of discovered sensor topics, grouped by viewer.
 *
 * Each topic has a button that opens it in a new panel. Topics without a
 * publisher are listed too and marked idle. With no sensor topics at all the
 * list says so; the rest of the tab keeps working.
 */
function renderDiscovered() {
  const groups = [];
  for (const [viewer, entry] of Object.entries(VIEWERS)) {
    const items = discovered.filter((item) => item.viewer === viewer);
    if (items.length === 0) continue;
    groups.push(h('h3', {}, entry.label), ...items.map((item) => h('div', {class: 'sensor-row', 'data-topic': item.topic},
      h('span', {class: 'name mono', title: `${item.topic}\n${item.type}`}, item.topic),
      item.publisher_count ? null : h('span', {class: 'muted'}, 'idle'),
      h('button', {class: 'action', onclick: () => addPanel(viewer, item.topic, item.type)}, 'Open'))));
  }
  listHost.replaceChildren(...(groups.length ? groups
    : [h('p', {class: 'empty', id: 'sensors-none'}, 'No sensor topics discovered. Other topics remain inspectable under ROS 2 → Topics.')]));
}

export default {
  id: 'sensors',
  title: 'Sensors',

  /**
   * Build the discovery list and the panel area, restore remembered panels, and start discovery.
   *
   * Discovery polls every three seconds and updates the list and every
   * panel's topic selector without disturbing running viewers.
   */
  mount(container) {
    listHost = h('div', {class: 'sensor-list', id: 'sensor-list'});
    panelHost = h('div', {class: 'panels', id: 'sensor-panels'});
    const picker = h('select', {id: 'add-viewer'}, Object.entries(VIEWERS).map(([id, entry]) => h('option', {value: id}, entry.label)));
    container.append(h('div', {class: 'view'},
      h('div', {class: 'sidebar column'},
        h('div', {class: 'toolbar'}, picker, h('button', {class: 'action', id: 'add-panel', onclick: () => addPanel(picker.value)}, 'Add panel')),
        listHost),
      panelHost));
    mounted = [];
    for (const config of remembered) mountPanel(config);
    renderHint();
    stopDiscovery = poll(async () => {
      discovered = (await getJSON('/sensors/topics')).topics;
      renderDiscovered();
      for (const panel of mounted) fillTopics(panel);
    }, 3000);
  },

  /** Stop discovery and close every panel's stream; the panel list is kept for the next visit. */
  unmount() {
    if (stopDiscovery) stopDiscovery();
    for (const panel of mounted) unmountPanel(panel);
    mounted = [];
    stopDiscovery = null;
  },
};
