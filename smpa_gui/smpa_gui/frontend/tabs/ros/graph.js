/**
 * Live ROS 2 computation graph, conceptually equivalent to rqt_graph.
 *
 * Concepts: the graph description comes from the gateway (nodes, topics,
 * publish and subscribe edges, actions). The desired elements are what
 * should be drawn for the current filters. On every refresh the drawing is
 * reconciled with the desired elements: vanished elements are removed and
 * new ones added, while existing ones keep their position. The zoom and pan
 * chosen by the user are never reset by a refresh.
 */
import {getJSON, h, poll} from '../../api.js';

const filters = {topics: true, infrastructure: false, hidden: false, gateway: false, leaves: true,
                 actions: true, search: ''};
let cy = null;
let stop = null;
let latest = null;
let side = null;
let summary = null;
let firstLayout = true;

/**
 * Decide which nodes, topics and connections to draw for the current filters.
 *
 * Start from all nodes and topics and leave out what a filter hides: the
 * gateway's own node, hidden names, infrastructure topics (/rosout and
 * /parameter_events), and topics nobody is connected to. Topics that
 * implement an action are replaced by one action element between its client
 * and server nodes. With topics switched off, each publisher is connected
 * directly to each subscriber of the same topic and the edge carries the
 * topic name, like the "nodes only" mode of rqt_graph.
 */
export function desiredElements(graph, options) {
  const elements = [];
  const nodes = graph.nodes.filter((node) => (options.gateway || !node.is_gateway) && (options.hidden || !node.hidden));
  const nodeIds = new Set(nodes.map((node) => node.id));
  for (const node of nodes) {
    elements.push({data: {id: node.id, label: node.full_name, kind: node.is_gateway ? 'gateway' : 'node', item: node}});
  }
  const topics = graph.topics.filter((topic) => {
    if (topic.action) return false;
    if (topic.infrastructure && !options.infrastructure) return false;
    if (topic.hidden && !options.hidden) return false;
    return true;
  });
  const edgesByTopic = new Map();
  for (const edge of graph.edges) {
    const topicId = edge.kind === 'publishes' ? edge.target : edge.source;
    const nodeId = edge.kind === 'publishes' ? edge.source : edge.target;
    if (!nodeIds.has(nodeId)) continue;
    if (!edgesByTopic.has(topicId)) edgesByTopic.set(topicId, {publishers: [], subscribers: []});
    edgesByTopic.get(topicId)[edge.kind === 'publishes' ? 'publishers' : 'subscribers'].push({nodeId, edge});
  }
  for (const topic of topics) {
    const ends = edgesByTopic.get(topic.id) || {publishers: [], subscribers: []};
    if (!options.leaves && (ends.publishers.length === 0 || ends.subscribers.length === 0)) continue;
    if (options.topics) {
      if (ends.publishers.length + ends.subscribers.length === 0) continue;
      elements.push({data: {id: topic.id, label: topic.name, kind: 'topic', item: topic}});
      for (const {edge} of [...ends.publishers, ...ends.subscribers]) {
        elements.push({data: {id: edge.id, source: edge.source, target: edge.target, kind: edge.kind, item: edge}});
      }
    } else {
      const seen = new Set();
      for (const publisher of ends.publishers) {
        for (const subscriber of ends.subscribers) {
          const id = `direct:${publisher.nodeId}>${topic.name}>${subscriber.nodeId}`;
          if (seen.has(id)) continue;
          seen.add(id);
          elements.push({data: {id, source: publisher.nodeId, target: subscriber.nodeId, label: topic.name,
                                kind: 'direct', item: topic}});
        }
      }
    }
  }
  if (options.actions) {
    for (const action of graph.actions) {
      const servers = action.servers.map((name) => 'node:' + name).filter((id) => nodeIds.has(id));
      const clients = action.clients.map((name) => 'node:' + name).filter((id) => nodeIds.has(id));
      if (servers.length + clients.length === 0) continue;
      elements.push({data: {id: action.id, label: action.name, kind: 'action', item: action}});
      for (const client of clients) {
        elements.push({data: {id: `${action.id}<${client}`, source: client, target: action.id, kind: 'action-edge'}});
      }
      for (const server of servers) {
        elements.push({data: {id: `${action.id}>${server}`, source: action.id, target: server, kind: 'action-edge'}});
      }
    }
  }
  return elements;
}

/** Estimate how wide a label is drawn, so boxes and columns fit their text. */
function labelWidth(label) {
  return Math.max(40, label.length * 6.7 + 18);
}

/**
 * Compute a left-to-right layered position for every element, like rqt_graph's drawing.
 *
 * Each element's layer is the length of the longest path leading to it, so
 * data flows from publishers on the left to subscribers on the right.
 * First, a walk along the connections finds those that close a loop; they
 * are ignored when layering, which keeps the result finite for graphs with
 * feedback. Within a layer,
 * elements are ordered by the average height of what feeds them, which
 * reduces crossings. Columns are as wide as their longest label.
 */
export function layeredPositions(nodes, edges) {
  const outgoing = new Map(nodes.map((node) => [node.id, []]));
  const usable = edges.filter((edge) => outgoing.has(edge.source) && outgoing.has(edge.target));
  for (const edge of usable) outgoing.get(edge.source).push(edge.target);
  const fed = new Set(usable.map((edge) => edge.target));
  const state = new Map();
  const loopClosing = new Set();
  /** Walk downstream from one element and mark every connection that leads back into the walk. */
  function walk(id) {
    state.set(id, 'open');
    for (const target of outgoing.get(id)) {
      if (state.get(target) === 'open') loopClosing.add(id + '\u0000' + target);
      else if (!state.has(target)) walk(target);
    }
    state.set(id, 'done');
  }
  for (const node of [...nodes.filter((item) => !fed.has(item.id)), ...nodes]) if (!state.has(node.id)) walk(node.id);
  const incoming = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of usable) {
    if (!loopClosing.has(edge.source + '\u0000' + edge.target)) incoming.get(edge.target).push(edge.source);
  }
  const layer = new Map();
  /** Return the layer of one element: one more than the deepest element feeding it. */
  function layerOf(id) {
    if (!layer.has(id)) layer.set(id, 1 + Math.max(-1, ...incoming.get(id).map(layerOf)));
    return layer.get(id);
  }
  for (const node of nodes) layerOf(node.id);
  const columns = [];
  for (const node of nodes) {
    const index = layer.get(node.id);
    (columns[index] = columns[index] || []).push(node);
  }
  const positions = new Map();
  let x = 0;
  for (const column of columns) {
    if (!column) continue;
    const height = (id) => (positions.has(id) ? positions.get(id).y : 0);
    const weight = (node) => {
      const feeders = incoming.get(node.id).filter((id) => positions.has(id));
      return feeders.length ? feeders.reduce((sum, id) => sum + height(id), 0) / feeders.length : Number.MAX_VALUE;
    };
    column.sort((a, b) => weight(a) - weight(b) || a.label.localeCompare(b.label));
    const width = Math.max(...column.map((node) => labelWidth(node.label)));
    column.forEach((node, index) => positions.set(node.id, {x: x + width / 2, y: (index - (column.length - 1) / 2) * 46}));
    x += width + 90;
  }
  return positions;
}

/**
 * Arrange the drawing in layers from publishers on the left to subscribers on the right.
 *
 * The viewport is fitted to the graph only for the very first layout. Later
 * layouts move elements but leave the user's zoom and pan alone.
 */
function layout() {
  if (!cy || cy.elements().length === 0) return;
  const positions = layeredPositions(cy.nodes().map((node) => ({id: node.id(), label: node.data('label')})),
    cy.edges().map((edge) => ({source: edge.source().id(), target: edge.target().id()})));
  cy.layout({name: 'preset', positions: (node) => positions.get(node.id()), fit: firstLayout, padding: 30, animate: false}).run();
  if (firstLayout && cy.zoom() > 1.2) { cy.zoom(1.2); cy.center(); }
  firstLayout = false;
}

/**
 * Make the drawing match the desired elements without disturbing what stays.
 *
 * Elements that are no longer desired are removed and missing ones are
 * added. The layout runs again only when the set of elements changed, so an
 * unchanged graph is not touched at all and a user's manual arrangement
 * survives quiet refreshes. Search highlighting is applied afterwards.
 */
function reconcile() {
  if (!cy || !latest) return;
  const desired = desiredElements(latest, filters);
  const wanted = new Map(desired.map((element) => [element.data.id, element]));
  const removed = cy.elements().filter((element) => !wanted.has(element.id()));
  removed.remove();
  const ordered = [...desired.filter((element) => !element.data.source), ...desired.filter((element) => element.data.source)];
  let added = 0;
  for (const element of ordered) {
    const existing = cy.getElementById(element.data.id);
    if (existing.length) existing.data('item', element.data.item);
    else { cy.add({group: element.data.source ? 'edges' : 'nodes', data: element.data}); added += 1; }
  }
  if (added || removed.length) layout();
  applySearch();
  summary.textContent = `${cy.nodes('[kind = "node"], [kind = "gateway"]').length} nodes · ` +
    `${cy.nodes('[kind = "topic"]').length} topics · ${cy.nodes('[kind = "action"]').length} actions`;
}

/**
 * Dim everything whose name does not contain the search text.
 *
 * Matching elements and the edges between them stay fully visible. An empty
 * search shows everything normally. Nothing is removed by searching.
 */
function applySearch() {
  const text = filters.search.trim().toLowerCase();
  cy.elements().removeClass('dim');
  if (!text) return;
  const matches = cy.nodes().filter((node) => node.data('label').toLowerCase().includes(text));
  cy.elements().not(matches).not(matches.edgesWith(matches)).addClass('dim');
}

/**
 * Describe the clicked element in the side panel.
 *
 * A node lists what it publishes and subscribes to. A topic lists its types
 * and offers a link to the Topics view. An action lists its types, servers
 * and clients. Clicking the background clears the panel.
 */
function inspect(element) {
  side.replaceChildren();
  if (!element) { side.append(h('p', {class: 'muted'}, 'Select a node, topic or action.')); return; }
  const item = element.data('item');
  const kind = element.data('kind');
  side.append(h('h2', {}, element.data('label')), h('p', {class: 'muted'}, kind));
  if (kind === 'node' || kind === 'gateway') {
    const published = latest.edges.filter((edge) => edge.kind === 'publishes' && edge.source === item.id);
    const subscribed = latest.edges.filter((edge) => edge.kind === 'subscribes' && edge.target === item.id);
    side.append(h('h3', {}, `Publishes (${published.length})`),
      h('ul', {}, published.map((edge) => h('li', {class: 'mono'}, edge.target.slice(6)))),
      h('h3', {}, `Subscribes (${subscribed.length})`),
      h('ul', {}, subscribed.map((edge) => h('li', {class: 'mono'}, edge.source.slice(6)))),
      h('p', {}, h('a', {href: '#ros/parameters/' + item.full_name}, 'Show parameters')));
  } else if (kind === 'topic' || kind === 'direct') {
    side.append(h('h3', {}, 'Types'), h('ul', {}, item.types.map((type) => h('li', {class: 'mono'}, type))),
      h('p', {}, `${item.publisher_count} publishers · ${item.subscriber_count} subscribers`),
      h('p', {}, h('a', {href: '#ros/topics/' + item.name}, 'Inspect in Topics')));
  } else if (kind === 'action') {
    side.append(h('h3', {}, 'Types'), h('ul', {}, item.types.map((type) => h('li', {class: 'mono'}, type))),
      h('h3', {}, 'Servers'), h('ul', {}, item.servers.map((name) => h('li', {class: 'mono'}, name))),
      h('h3', {}, 'Clients'), h('ul', {}, item.clients.map((name) => h('li', {class: 'mono'}, name))));
  }
}

/** Build one labelled checkbox bound to a filter and redraw when it changes. */
function toggle(key, label) {
  return h('label', {}, h('input', {type: 'checkbox', checked: filters[key], 'data-filter': key,
    onchange: (event) => { filters[key] = event.target.checked; reconcile(); }}), label);
}

export default {
  id: 'graph',
  title: 'Graph',

  /**
   * Build the toolbar, the drawing area and the detail panel, then start refreshing.
   *
   * The graph description is fetched every two seconds. While the gateway
   * cannot be reached the last drawing stays on screen and the summary says so.
   */
  mount(container) {
    summary = h('span', {class: 'count', id: 'graph-summary'}, 'loading…');
    const toolbar = h('div', {class: 'toolbar'},
      h('input', {type: 'search', placeholder: 'Search names…', id: 'graph-search', value: filters.search,
                  oninput: (event) => { filters.search = event.target.value; applySearch(); }}),
      toggle('topics', 'Topics'), toggle('actions', 'Actions'), toggle('leaves', 'Unconnected topics'),
      toggle('infrastructure', '/rosout, /parameter_events'), toggle('hidden', 'Hidden names'),
      toggle('gateway', 'This GUI'),
      h('button', {class: 'action', onclick: () => cy.fit(undefined, 30)}, 'Fit'),
      h('button', {class: 'action', onclick: () => layout()}, 'Re-arrange'), summary);
    const canvas = h('div', {id: 'graph-canvas'});
    side = h('aside', {class: 'graph-side'});
    container.append(h('div', {class: 'column', style: 'flex:1'}, toolbar, canvas), side);
    const text = getComputedStyle(document.body).getPropertyValue('--text').trim();
    const line = getComputedStyle(document.body).getPropertyValue('--muted').trim();
    cy = window.cytoscape({
      container: canvas, minZoom: 0.05, maxZoom: 4,
      style: [
        {selector: 'node', style: {label: 'data(label)', 'font-size': 11, color: text, 'text-valign': 'center',
          'text-halign': 'center', width: (node) => labelWidth(node.data('label')), height: 28, 'border-width': 1.5,
          'background-color': '#dbe9ff', 'border-color': '#1f6feb', shape: 'ellipse', 'text-outline-width': 0}},
        {selector: 'node[kind = "node"], node[kind = "gateway"]', style: {color: '#10233f'}},
        {selector: 'node[kind = "gateway"]', style: {'background-color': '#e5e7eb', 'border-color': '#8b95a5', 'border-style': 'dashed'}},
        {selector: 'node[kind = "topic"]', style: {shape: 'round-rectangle', 'background-color': '#e9f7ec', 'border-color': '#1a7f37', color: '#0f3d1c'}},
        {selector: 'node[kind = "action"]', style: {shape: 'cut-rectangle', 'background-color': '#fff1d6', 'border-color': '#9a6700', color: '#4d3300'}},
        {selector: 'edge', style: {width: 1.4, 'line-color': line, 'target-arrow-color': line, 'target-arrow-shape': 'triangle',
          'curve-style': 'bezier', 'arrow-scale': 0.9}},
        {selector: 'edge[label]', style: {label: 'data(label)', 'font-size': 9, color: text, 'text-rotation': 'autorotate',
          'text-background-color': '#ffffff', 'text-background-opacity': 0.01}},
        {selector: 'edge[kind = "action-edge"]', style: {'line-style': 'dashed', 'line-color': '#9a6700', 'target-arrow-color': '#9a6700'}},
        {selector: '.dim', style: {opacity: 0.15}},
        {selector: ':selected', style: {'border-width': 3, 'line-color': '#1f6feb', 'target-arrow-color': '#1f6feb'}},
      ],
    });
    window.__graph = cy;
    cy.on('tap', (event) => inspect(event.target === cy ? null : event.target));
    inspect(null);
    firstLayout = true;
    stop = poll(async () => { latest = await getJSON('/ros/graph'); reconcile(); }, 2000,
      (error) => { summary.textContent = 'gateway unreachable – showing last known graph'; });
  },

  /** Stop refreshing and release the drawing. */
  unmount() {
    if (stop) stop();
    if (cy) cy.destroy();
    cy = stop = latest = null;
  },
};
