/**
 * The ROS 2 Workspace: discovered interfaces arranged around an empty canvas.
 *
 * Concepts: the workspace has five regions. Topics run along the top,
 * services down the left, actions down the right and parameters along the
 * bottom; the canvas in the middle takes the remaining space. The canvas is
 * reserved for the future visual ROS 2 editor and stays empty.
 *
 * An interface is one discovered topic, service, action or parameter. It is
 * shown as a chip: a small rectangle with its name and type, coloured by its
 * kind. Each chip carries data attributes (data-kind, data-name, data-type,
 * and data-node for parameters) that fully describe the interface, so a
 * future editor can pick chips up and place them on the canvas without
 * asking the gateway again. Clicking a chip opens the existing inspector:
 * the Topics, Services or Parameters view of this tab, or the Actions tab.
 *
 * A panel is one of the four regions. Each panel polls one existing gateway
 * route (the same routes the other views use), filters what it got by the
 * text typed in its search field, and redraws only when what it shows has
 * changed, so scrolling positions survive refreshes. Panels can be
 * collapsed and resized; the sizes are remembered in this browser.
 *
 * Availability: an interface whose provider is missing (a topic without
 * publishers, a service without a provider, an action without a server, a
 * node that does not answer parameter requests) is shown dimmed with the
 * reason. An interface that disappears from discovery is shown as gone,
 * crossed out, for GONE_MS and then removed. When the gateway cannot be
 * reached, the panels keep the last known interfaces, dimmed, and say so.
 *
 * Nothing here changes the ROS graph. Parameters are changed only in the
 * Parameters view, with its confirmation dialog.
 */
import {getJSON, h, poll} from '../../api.js';
import {createCanvas} from './workspace_canvas.js';

const GONE_MS = 10000;
const LAYOUT_KEY = 'smpa-gui.workspace.layout';
const VIEWPORT_KEY = 'smpa-gui.workspace.viewport';
const COLLAPSED_SIZE = 34;
const DEFAULT_LAYOUT = {left: 270, right: 270, top: 150, bottom: 210, grid: true, wrapTopics: true,
  collapsed: {topics: false, services: false, actions: false, parameters: false}};
const PARAMETER_STATE_TEXT = {not_responding: 'not responding', unavailable: 'no parameter services', error: 'could not be read',
  gone: 'node gone'};

let layout = loadLayout();
let viewport = loadViewport();
let canvas = null;
let root = null;
let stops = [];
let resizeObserver = null;
const panels = {};

/**
 * Read a JSON value from browser storage, or return the fallback.
 *
 * Browser storage may be missing, full, blocked or contain something
 * unreadable; in all those cases the fallback is used and the workspace
 * still works, it just does not remember its arrangement.
 */
function stored(storage, key, fallback) {
  try {
    const text = window[storage].getItem(key);
    return text ? JSON.parse(text) : fallback;
  } catch (error) {
    return fallback;
  }
}

/** Write a JSON value to browser storage, ignoring a storage that refuses it. */
function store(storage, key, value) {
  try { window[storage].setItem(key, JSON.stringify(value)); } catch (error) { /* not remembered */ }
}

/** Load the remembered panel sizes and switches, completed with the defaults for anything missing. */
function loadLayout() {
  const saved = stored('localStorage', LAYOUT_KEY, {});
  return {...DEFAULT_LAYOUT, ...saved, collapsed: {...DEFAULT_LAYOUT.collapsed, ...(saved.collapsed || {})}};
}

/** Load the canvas viewport remembered for this browser tab, or null to start from the reset view. */
function loadViewport() {
  return stored('sessionStorage', VIEWPORT_KEY, null);
}

/**
 * Apply the layout to the workspace: region sizes, collapsed regions and the grid switch.
 *
 * Sizes are CSS variables of the workspace's grid. A collapsed region
 * shrinks to its header. The layout is then remembered in this browser.
 */
function applyLayout() {
  if (!root) return;
  const size = (region, value) => (layout.collapsed[region] ? COLLAPSED_SIZE : value) + 'px';
  root.style.setProperty('--ws-left', size('services', layout.left));
  root.style.setProperty('--ws-right', size('actions', layout.right));
  root.style.setProperty('--ws-top', size('topics', layout.top));
  root.style.setProperty('--ws-bottom', size('parameters', layout.bottom));
  for (const [region, collapsed] of Object.entries(layout.collapsed)) {
    const panel = panels[region];
    if (!panel) continue;
    panel.element.classList.toggle('collapsed', collapsed);
    panel.collapse.setAttribute('aria-expanded', String(!collapsed));
    panel.collapse.textContent = collapsed ? '▸' : '▾';
  }
  if (panels.topics) panels.topics.body.classList.toggle('ws-nowrap', !layout.wrapTopics);
  store('localStorage', LAYOUT_KEY, layout);
}

/**
 * Remember which interfaces were seen, so that disappearing ones can be shown as gone for a while.
 *
 * ``memory`` maps each interface key to its last description and the time
 * it disappeared (null while it is present). Every discovered item
 * refreshes its entry. An entry that is no longer discovered is stamped
 * with the time it disappeared, and dropped once it has been gone for
 * GONE_MS. The entries are returned sorted by key, each with a ``gone``
 * flag.
 */
function track(memory, items, key) {
  const now = Date.now();
  const present = new Set();
  for (const item of items) {
    present.add(key(item));
    memory.set(key(item), {item, goneSince: null});
  }
  for (const [name, entry] of memory) {
    if (present.has(name)) continue;
    if (entry.goneSince === null) entry.goneSince = now;
    else if (now - entry.goneSince > GONE_MS) memory.delete(name);
  }
  return [...memory.entries()].sort(([a], [b]) => a.localeCompare(b))
    .map(([, entry]) => ({...entry.item, gone: entry.goneSince !== null}));
}

/** Tell whether an interface matches the filter text by any of the given texts (case-insensitive). */
function matches(text, ...candidates) {
  const wanted = text.trim().toLowerCase();
  return !wanted || candidates.some((candidate) => String(candidate).toLowerCase().includes(wanted));
}

/** Show a parameter value compactly: strings and lists as JSON, at most 60 characters. */
function valueText(value) {
  const text = value === null || value === undefined ? '–'
    : typeof value === 'object' || typeof value === 'string' ? JSON.stringify(value) : String(value);
  return text.length > 60 ? text.slice(0, 59) + '…' : text;
}

/**
 * Build one interface chip.
 *
 * ``kind`` is topic, service, action or parameter; ``name`` and ``type``
 * identify the interface; ``state`` is "ok", a reason why it is unavailable,
 * or "gone". ``lines`` are the texts shown under the name. ``extra`` adds
 * further data attributes. Clicking the chip goes to ``target``, the
 * address of the existing inspector for it.
 */
function chip({kind, name, type, state, lines, title, target, extra = {}}) {
  return h('button', {type: 'button', class: `ws-chip ws-${kind}`, 'data-kind': kind, 'data-name': name, 'data-type': type,
    'data-state': state, title, draggable: 'false', onclick: () => { location.hash = target; }, ...extra},
  h('span', {class: 'ws-chip-name mono'}, name), lines.map((line) => line && h('span', {class: 'ws-chip-line'}, line)));
}

/** Describe availability in a few words for a chip's tooltip and status line. */
function stateWord(state) {
  return {ok: '', gone: 'gone from discovery', no_publisher: 'no publisher', unavailable: 'no provider'}[state] ?? state;
}

/** Build the chip of one topic: name, message type and its publisher and subscriber counts. */
function topicChip(topic) {
  const state = topic.gone ? 'gone' : topic.publisher_count === 0 ? 'no_publisher' : 'ok';
  const type = topic.types.join(', ');
  return chip({kind: 'topic', name: topic.name, type, state, target: 'ros/topics/' + topic.name,
    title: `${topic.name}\n${type}\n${topic.publisher_count} publishers, ${topic.subscriber_count} subscribers` +
      (state === 'ok' ? '' : `\n${stateWord(state)}`),
    lines: [`${type} · ${topic.publisher_count}▸ ${topic.subscriber_count}◂` + (state === 'ok' ? '' : ` · ${stateWord(state)}`)]});
}

/** Build the chip of one service: name, service type and its providers. */
function serviceChip(service) {
  const state = service.gone ? 'gone' : service.providers.length === 0 ? 'unavailable' : 'ok';
  const type = service.types.join(', ');
  return chip({kind: 'service', name: service.name, type, state, target: 'ros/services/' + service.name,
    title: `${service.name}\n${type}\nprovided by ${service.providers.join(', ') || 'nobody'}` + (state === 'ok' ? '' : `\n${stateWord(state)}`),
    lines: [type, state === 'ok' ? null : stateWord(state)]});
}

/** Build the chip of one action: name, action type and its servers. */
function actionChip(action) {
  const state = action.gone ? 'gone' : action.servers.length === 0 ? 'unavailable' : 'ok';
  const type = action.types.join(', ');
  return chip({kind: 'action', name: action.name, type, state, target: 'actions/' + action.name,
    title: `${action.name}\n${type}\nserved by ${action.servers.join(', ') || 'nobody'}` + (state === 'ok' ? '' : `\n${stateWord(state === 'unavailable' ? 'no server' : state)}`),
    lines: [type, state === 'ok' ? null : (state === 'unavailable' ? 'no server' : stateWord(state))]});
}

/** Build the chip of one parameter of one node: name, current value and type. */
function parameterChip(node, parameter, gone) {
  return chip({kind: 'parameter', name: parameter.name, type: parameter.type, state: gone ? 'gone' : 'ok',
    target: `ros/parameters/${node}@${parameter.name}`, extra: {'data-node': node, 'data-value': JSON.stringify(parameter.value)},
    title: `${node}: ${parameter.name}\n${parameter.type} = ${valueText(parameter.value)}` + (parameter.read_only ? '\nread-only' : '')
      + (parameter.description ? `\n${parameter.description}` : ''),
    lines: [h('span', {class: 'mono ws-value'}, valueText(parameter.value)), parameter.type + (parameter.read_only ? ' · read-only' : '')]});
}

/**
 * Build the group of one node in the parameter panel: the node's name, its state and its parameter chips.
 *
 * A node that did not answer, offers no parameter services or disappeared
 * shows that state instead of chips (or with its last known chips, when it
 * disappeared), so every parameter stays visibly attached to its node.
 */
function parameterGroup(reading, chips) {
  const state = reading.gone ? 'gone' : reading.state;
  return h('section', {class: 'ws-group', 'data-node': reading.node, 'data-state': state},
    h('header', {class: 'ws-group-head'}, h('span', {class: 'mono'}, reading.node),
      reading.is_gateway ? h('span', {class: 'muted'}, ' (this GUI)') : null,
      h('span', {class: 'ws-group-state muted'}, state === 'ok' ? `${reading.parameters.length}` : PARAMETER_STATE_TEXT[state] || state)),
    chips.length ? h('div', {class: 'ws-group-chips'}, chips) : null);
}

/**
 * The four panels: what each polls, how it filters, and how it draws its interfaces.
 *
 * ``fetch`` asks the gateway; ``key`` names an interface uniquely;
 * ``render`` returns the panel's content for the tracked interfaces and the
 * filter, together with how many are shown out of how many known.
 */
const PANELS = {
  topics: {
    title: 'Topics', every: 2000, placeholder: 'Filter topics…', options: {hidden: false},
    fetch: async () => (await getJSON('/ros/topics')).topics,
    key: (topic) => topic.name,
    render(items, text, options) {
      const shown = items.filter((topic) => (options.hidden || (!topic.hidden && !topic.action))
        && matches(text, topic.name, ...topic.types));
      return {shown: shown.length, content: shown.map(topicChip)};
    },
  },
  services: {
    title: 'Services', every: 2000, placeholder: 'Filter services…', options: {plumbing: false},
    fetch: async () => (await getJSON('/ros/services')).services,
    key: (service) => service.name,
    render(items, text, options) {
      const shown = items.filter((service) => (options.plumbing || (service.kind === 'other' && !service.hidden))
        && matches(text, service.name, ...service.types));
      return {shown: shown.length, content: shown.map(serviceChip)};
    },
  },
  actions: {
    title: 'Actions', every: 2000, placeholder: 'Filter actions…', options: {},
    fetch: async () => (await getJSON('/ros/actions')).actions,
    key: (action) => action.name,
    render(items, text) {
      const shown = items.filter((action) => matches(text, action.name, ...action.types));
      return {shown: shown.length, content: shown.map(actionChip)};
    },
  },
  parameters: {
    title: 'Parameters', every: 5000, placeholder: 'Filter by node, parameter or value…', options: {},
    fetch: async () => (await getJSON('/ros/parameters')).nodes,
    key: (reading) => reading.node,
    render(items, text) {
      let shown = 0;
      let total = 0;
      const groups = [];
      for (const reading of items) {
        total += reading.parameters.length;
        const nodeMatches = matches(text, reading.node);
        const parameters = reading.parameters.filter((parameter) => nodeMatches
          || matches(text, parameter.name, valueText(parameter.value), parameter.type));
        if (!nodeMatches && !parameters.length) continue;
        shown += parameters.length;
        groups.push(parameterGroup(reading, parameters.map((parameter) => parameterChip(reading.node, parameter, reading.gone))));
      }
      return {shown, total, content: groups};
    },
  },
};

/**
 * Draw one panel from its tracked interfaces, unless nothing visible has changed.
 *
 * The content is computed for the current filter and switches. A
 * signature of what would be shown (the interfaces with their states, the
 * filter and whether the gateway is reachable) is compared with the last
 * one; only a different signature replaces the chips, so the panel does not
 * flicker and keeps its scroll position while ROS stays the same.
 */
function draw(region) {
  const panel = panels[region];
  const spec = PANELS[region];
  const signature = JSON.stringify([panel.items, panel.filter.value, panel.options, panel.offline, panel.error]);
  if (signature === panel.signature) return;
  panel.signature = signature;
  const result = spec.render(panel.items, panel.filter.value, panel.options);
  const total = result.total ?? panel.items.length;
  panel.count.textContent = `${result.shown} of ${total}`;
  panel.element.toggleAttribute('data-offline', panel.offline);
  panel.status.textContent = panel.offline ? 'gateway unreachable · last known' : panel.error || '';
  panel.body.replaceChildren(...(result.content.length ? result.content
    : [h('p', {class: 'ws-empty muted'}, panel.loaded ? (panel.items.length ? 'Nothing matches the filter.' : `No ${spec.title.toLowerCase()} discovered.`) : 'Discovering…')]));
}

/**
 * Ask the gateway for one panel's interfaces and redraw the panel.
 *
 * A successful answer is merged into the panel's memory, which marks
 * disappeared interfaces as gone. A failed answer leaves the last known
 * interfaces in place and marks the panel as offline, so the user can tell
 * stale data from current data. A refresh that is already running is not
 * started a second time, and a collapsed panel is not refreshed at all, so
 * a hidden panel puts no load on the gateway or on the ROS nodes; it is
 * refreshed again as soon as it is expanded.
 */
async function refresh(region) {
  const panel = panels[region];
  if (!panel || panel.busy || layout.collapsed[region]) return;
  panel.busy = true;
  try {
    const items = await PANELS[region].fetch();
    panel.items = track(panel.memory, items, PANELS[region].key);
    panel.offline = false;
    panel.error = '';
    panel.loaded = true;
  } catch (error) {
    panel.offline = true;
  } finally {
    panel.busy = false;
  }
  if (panels[region] === panel) draw(region);
}

/**
 * Build one panel: header with collapse button, title, count, filter, switches and status, then the chip area.
 *
 * The panel keeps its own state: the tracked interfaces, the filter text
 * and switches, and whether the gateway answered. Typing in the filter or
 * flipping a switch redraws immediately, without asking the gateway.
 */
function buildPanel(region) {
  const spec = PANELS[region];
  const previous = panels[region];
  const panel = {
    memory: previous ? previous.memory : new Map(), items: previous ? previous.items : [], loaded: previous ? previous.loaded : false,
    options: previous ? previous.options : {...spec.options}, offline: false, error: '', signature: null, busy: false,
  };
  panel.filter = h('input', {type: 'search', class: 'ws-filter', placeholder: spec.placeholder, 'aria-label': spec.placeholder,
    value: previous ? previous.filter.value : '', oninput: () => draw(region)});
  panel.count = h('span', {class: 'count ws-count'}, '');
  panel.status = h('span', {class: 'ws-status muted'}, '');
  panel.collapse = h('button', {type: 'button', class: 'ws-collapse', title: `Collapse or expand ${spec.title}`, 'aria-expanded': 'true',
    onclick: () => {
      layout.collapsed[region] = !layout.collapsed[region];
      applyLayout();
      if (!layout.collapsed[region]) refresh(region);
    }}, '▾');
  const switches = Object.keys(panel.options).map((option) => h('label', {class: 'ws-switch'},
    h('input', {type: 'checkbox', 'data-option': option, checked: panel.options[option],
      onchange: (event) => { panel.options[option] = event.target.checked; draw(region); }}),
    {hidden: 'Hidden', plumbing: 'Plumbing'}[option]));
  if (region === 'topics') {
    switches.push(h('label', {class: 'ws-switch'}, h('input', {type: 'checkbox', id: 'workspace-wrap-topics', checked: layout.wrapTopics,
      onchange: (event) => { layout.wrapTopics = event.target.checked; applyLayout(); }}), 'Wrap'));
  }
  if (region === 'parameters') {
    switches.push(h('button', {type: 'button', class: 'action ws-refresh', id: 'workspace-parameters-refresh',
      title: 'Read all parameters again now', onclick: () => refresh('parameters')}, 'Refresh'));
  }
  panel.body = h('div', {class: 'ws-body', id: `workspace-${region}-list`});
  panel.element = h('section', {class: `ws-panel ws-panel-${region}`, 'data-panel': region, style: `grid-area:${region}`},
    h('header', {class: 'ws-panel-head'}, panel.collapse, h('strong', {class: 'ws-title'}, spec.title), panel.count,
      panel.filter, switches, panel.status),
    panel.body);
  panels[region] = panel;
  return panel.element;
}

/**
 * Build one splitter between the canvas and a panel, which resizes that panel when dragged.
 *
 * ``edge`` names the panel's side: left (services), right (actions), top
 * (topics) or bottom (parameters). Dragging with the mouse, a pen or a
 * finger sets the panel's size from the pointer position; the arrow keys
 * change it in steps of 16 pixels when the splitter has the focus. Sizes
 * are kept between 80 pixels and 60 % of the workspace, so the canvas
 * always keeps most of the screen. A collapsed panel is expanded first.
 */
function splitter(edge) {
  const region = {left: 'services', right: 'actions', top: 'topics', bottom: 'parameters'}[edge];
  const vertical = edge === 'left' || edge === 'right';
  const element = h('div', {class: `ws-split ws-split-${edge}`, role: 'separator', tabindex: '0', 'data-edge': edge,
    'aria-orientation': vertical ? 'vertical' : 'horizontal', 'aria-label': `Resize ${PANELS[region].title}`, style: `grid-area:split-${edge}`});

  /** Set the panel's size, kept within the allowed range, and apply it; a collapsed panel is expanded and refreshed. */
  function setSize(value) {
    const box = root.getBoundingClientRect();
    const highest = (vertical ? box.width : box.height) * 0.6;
    const wasCollapsed = layout.collapsed[region];
    layout[edge] = Math.round(Math.max(80, Math.min(highest, value)));
    layout.collapsed[region] = false;
    applyLayout();
    if (wasCollapsed) refresh(region);
  }

  element.addEventListener('pointerdown', (event) => {
    element.setPointerCapture(event.pointerId);
    element.classList.add('dragging');
    const move = (moved) => {
      const box = root.getBoundingClientRect();
      setSize({left: moved.clientX - box.left, right: box.right - moved.clientX,
        top: moved.clientY - box.top, bottom: box.bottom - moved.clientY}[edge]);
    };
    const end = () => {
      element.classList.remove('dragging');
      element.removeEventListener('pointermove', move);
      element.removeEventListener('pointerup', end);
      element.removeEventListener('pointercancel', end);
    };
    element.addEventListener('pointermove', move);
    element.addEventListener('pointerup', end);
    element.addEventListener('pointercancel', end);
    event.preventDefault();
  });
  element.addEventListener('keydown', (event) => {
    const grow = {left: {ArrowRight: 16, ArrowLeft: -16}, right: {ArrowLeft: 16, ArrowRight: -16},
      top: {ArrowDown: 16, ArrowUp: -16}, bottom: {ArrowUp: 16, ArrowDown: -16}}[edge][event.key];
    if (!grow) return;
    setSize(layout[edge] + grow);
    event.preventDefault();
  });
  return element;
}

/**
 * Build the canvas region: the empty canvas, its toolbar and a hint that it is intentionally empty.
 *
 * The toolbar zooms in and out, resets, fits and switches the grid; it
 * shows the zoom in percent. The viewport is remembered for this browser
 * tab whenever it changes, so returning from an inspector shows the canvas
 * exactly as it was left.
 */
function buildCanvas(region) {
  const zoomText = h('span', {class: 'count', id: 'workspace-zoom'}, '');
  const stage = h('div', {class: 'ws-stage'});
  const button = (id, label, title, action) => h('button', {type: 'button', class: 'action', id, title, onclick: action}, label);
  const gridSwitch = h('input', {type: 'checkbox', id: 'workspace-grid', checked: layout.grid,
    onchange: (event) => { layout.grid = event.target.checked; canvas.setGrid(layout.grid); applyLayout(); }});
  region.append(
    h('div', {class: 'ws-canvas-tools'},
      button('workspace-zoom-out', '−', 'Zoom out (−)', () => canvas.zoomBy(0.8)), zoomText,
      button('workspace-zoom-in', '+', 'Zoom in (+)', () => canvas.zoomBy(1.25)),
      button('workspace-reset', 'Reset', 'Origin in the middle at 100 % (0)', () => canvas.reset()),
      button('workspace-fit', 'Fit', 'Fit the canvas content, or the sheet while it is empty (F)', () => canvas.fit()),
      h('label', {class: 'ws-switch'}, gridSwitch, 'Grid')),
    stage);
  canvas = createCanvas(stage, {initial: viewport, showGrid: layout.grid, onChange: (current) => {
    viewport = current;
    zoomText.textContent = `${Math.round(current.zoom * 100)} %`;
    store('sessionStorage', VIEWPORT_KEY, current);
  }});
  zoomText.textContent = `${Math.round(canvas.viewport().zoom * 100)} %`;
  stage.append(h('div', {class: 'ws-canvas-hint', id: 'workspace-canvas-hint'},
    'Empty canvas, reserved for the visual ROS 2 editor. Drag to pan, scroll to zoom.'));
}

export default {
  id: 'workspace',
  title: 'Workspace',

  /**
   * Build the five regions and start discovering.
   *
   * The panels, splitters and canvas are created, the remembered layout is
   * applied, and each panel starts polling its gateway route. The panels
   * remember their interfaces and filters across visits during this page
   * visit, so coming back from an inspector shows them immediately; the
   * canvas comes back with the viewport it had.
   */
  mount(container) {
    root = h('div', {class: 'ws-root', id: 'ros-workspace'});
    const canvasRegion = h('section', {class: 'ws-canvas-region', 'data-panel': 'canvas', style: 'grid-area:canvas'});
    root.append(buildPanel('topics'), buildPanel('services'), buildPanel('actions'), buildPanel('parameters'),
      splitter('left'), splitter('right'), splitter('top'), splitter('bottom'), canvasRegion);
    container.append(root);
    applyLayout();
    buildCanvas(canvasRegion);
    resizeObserver = new ResizeObserver(() => { if (canvas) canvas.resize(); });
    resizeObserver.observe(canvasRegion);
    for (const region of Object.keys(PANELS)) {
      draw(region);
      stops.push(poll(() => refresh(region), PANELS[region].every));
    }
  },

  /** Stop polling and release the canvas; the layout, filters, interfaces and viewport are kept for the next visit. */
  unmount() {
    for (const stop of stops) stop();
    stops = [];
    if (resizeObserver) resizeObserver.disconnect();
    resizeObserver = null;
    if (canvas) canvas.destroy();
    canvas = null;
    root = null;
  },
};
