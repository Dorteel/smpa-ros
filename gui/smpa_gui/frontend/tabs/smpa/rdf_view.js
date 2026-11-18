/**
 * An interactive drawing of one RDF graph: nodes, labelled edges, zoom, pan and node inspection.
 *
 * Concepts: the gateway turns a Turtle file into resources (nodes) and
 * resource-to-resource triples (edges); literal values stay attached to their
 * subject and are shown in the inspector. Nothing here knows what any
 * vocabulary means: every node and every predicate is drawn the same way,
 * except that blank nodes are dashed. A new version of the file is merged
 * into the drawing (added, removed and changed elements) so the viewport and
 * the positions of unchanged nodes stay where they were.
 */
import {h} from '../../api.js';

/** Estimate a node's width from its label so the text fits. */
function labelWidth(label) {
  return Math.max(46, Math.min(260, 12 + 6.6 * String(label || '').length));
}

/**
 * Create a drawing inside a container and return its controls.
 *
 * The returned object has show(graph) to merge a new graph, clear(), fit(),
 * arrange(), selected() and destroy(). onInspect(node or null) is called when
 * the user taps a node or the background.
 */
export function createRdfView(container, onInspect) {
  const style = getComputedStyle(document.body);
  const text = style.getPropertyValue('--text').trim() || '#1c2430';
  const line = style.getPropertyValue('--muted').trim() || '#667085';
  const cy = window.cytoscape({
    container, minZoom: 0.05, maxZoom: 4, wheelSensitivity: 0.3,
    style: [
      {selector: 'node', style: {label: 'data(label)', 'font-size': 11, color: '#10233f', 'text-valign': 'center',
        'text-halign': 'center', width: (node) => labelWidth(node.data('label')), height: 28, shape: 'round-rectangle',
        'background-color': '#dbe9ff', 'border-color': '#1f6feb', 'border-width': 1.5}},
      {selector: 'node[?blank]', style: {'border-style': 'dashed', 'background-color': '#eef1f5', 'border-color': '#8b95a5'}},
      {selector: 'edge', style: {width: 1.3, 'line-color': line, 'target-arrow-color': line, 'target-arrow-shape': 'triangle',
        'curve-style': 'bezier', 'arrow-scale': 0.9, label: 'data(label)', 'font-size': 9, color: text,
        'text-rotation': 'autorotate', 'text-margin-y': -7}},
      {selector: '.dim', style: {opacity: 0.15}},
      {selector: 'node:selected', style: {'border-width': 3, 'border-color': '#d97706'}},
      {selector: 'edge:selected', style: {width: 3, 'line-color': '#d97706', 'target-arrow-color': '#d97706'}},
    ],
  });
  let drawn = false;
  // Banners and panels above the drawing change its size; keep Cytoscape's idea of it current.
  const resizing = new ResizeObserver(() => cy.resize());
  resizing.observe(container);

  /** Lay the graph out; the first layout fits it to the view, later ones keep the viewport. */
  function arrange(fit = true, randomize = true) {
    if (!cy.nodes().length) return;
    cy.resize();
    cy.layout({name: 'cose', animate: false, randomize, fit, padding: 30, nodeRepulsion: () => 9000,
      idealEdgeLength: () => 90, numIter: 1500}).run();
  }

  cy.on('tap', (event) => {
    const target = event.target;
    if (target === cy) { cy.elements().removeClass('dim'); onInspect(null); return; }
    cy.elements().addClass('dim');
    if (target.isEdge()) {
      target.union(target.connectedNodes()).removeClass('dim');
      onInspect({kind: 'edge', edge: target.data('edge'), source: {id: target.source().id(), label: target.source().data('label')},
        target: {id: target.target().id(), label: target.target().data('label')}});
      return;
    }
    target.closedNeighborhood().removeClass('dim');
    onInspect(target.data('node'));
  });

  return {
    cy,
    /** Merge a new graph into the drawing; lay out only when nodes were added. */
    show(graph) {
      const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
      const edges = new Map(graph.edges.map((edge) => [edge.id, edge]));
      let added = 0;
      cy.batch(() => {
        cy.elements().forEach((element) => {
          const keep = element.isNode() ? nodes.has(element.id()) : edges.has(element.id());
          if (!keep) element.remove();
        });
        for (const node of nodes.values()) {
          const data = {id: node.id, label: node.label, blank: node.blank, node};
          const existing = cy.getElementById(node.id);
          if (existing.nonempty()) existing.data(data);
          else { cy.add({group: 'nodes', data}); added += 1; }
        }
        for (const edge of edges.values()) {
          if (cy.getElementById(edge.id).nonempty()) continue;
          cy.add({group: 'edges', data: {id: edge.id, source: edge.source, target: edge.target, label: edge.label, edge}});
        }
      });
      if (!drawn) { arrange(true, true); drawn = true; }
      else if (added) {
        const fresh = cy.nodes().filter((node) => node.position('x') === 0 && node.position('y') === 0);
        arrange(false, fresh.length === cy.nodes().length);
      }
      return added;
    },
    /** Remove everything; the next graph is laid out and fitted afresh. */
    clear() { cy.elements().remove(); drawn = false; onInspect(null); },
    fit() { cy.resize(); cy.fit(undefined, 30); },
    arrange: () => arrange(true, true),
    /** Describe a node's incoming and outgoing edges for the inspector. */
    links(nodeId) {
      const node = cy.getElementById(nodeId);
      if (node.empty()) return {incoming: [], outgoing: []};
      return {
        outgoing: node.outgoers('edge').map((edge) => ({predicate: edge.data('edge').predicate, label: edge.data('label'), other: edge.target().data('label'), otherId: edge.target().id()})),
        incoming: node.incomers('edge').map((edge) => ({predicate: edge.data('edge').predicate, label: edge.data('label'), other: edge.source().data('label'), otherId: edge.source().id()})),
      };
    },
    /** Select and centre a node by id, as if it had been tapped. */
    focus(nodeId) {
      const node = cy.getElementById(nodeId);
      if (node.empty()) return;
      cy.elements().unselect();
      node.select();
      cy.animate({center: {eles: node}}, {duration: 200});
      node.emit('tap');
    },
    destroy() { resizing.disconnect(); cy.destroy(); },
  };
}

/**
 * Build the inspector content for what was tapped: a node (IRI, literal values and links) or an edge (its triple).
 *
 * An edge is described by its predicate IRI and the subject and object it
 * connects; following either end selects that node.
 */
export function inspector(node, links, onFollow) {
  if (!node) return [h('p', {class: 'muted'}, 'Tap a node or an edge to inspect it. Scroll to zoom, drag the background to pan.')];
  if (node.kind === 'edge') {
    const follow = (end) => h('a', {href: '#', onclick: (event) => { event.preventDefault(); onFollow(end.id); }}, end.label);
    return [
      h('h2', {id: 'inspected-label'}, node.edge.label),
      h('div', {class: 'mono', style: 'word-break:break-all;font-size:12px', id: 'inspected-iri'}, node.edge.predicate),
      h('h3', {}, 'Triple'),
      h('dl', {class: 'readout', id: 'inspected-triple', style: 'padding:0'},
        h('dt', {}, 'Subject'), h('dd', {}, follow(node.source)), h('dt', {}, 'Predicate'), h('dd', {}, node.edge.label),
        h('dt', {}, 'Object'), h('dd', {}, follow(node.target))),
    ];
  }
  return [
    h('h2', {id: 'inspected-label'}, node.label),
    h('div', {class: 'mono', style: 'word-break:break-all;font-size:12px', id: 'inspected-iri'}, node.blank ? 'blank node' : node.iri),
    h('h3', {}, `Values (${node.literals.length})`),
    node.literals.length ? h('dl', {class: 'readout', id: 'inspected-literals', style: 'padding:0'},
      node.literals.flatMap((item) => [h('dt', {title: item.predicate}, item.predicate_label),
        h('dd', {title: item.datatype || (item.language ? '@' + item.language : '')}, item.value)])) : h('p', {class: 'muted'}, 'None'),
    h('h3', {}, `Outgoing (${links.outgoing.length})`),
    h('ul', {class: 'link-list'}, links.outgoing.map((link) => h('li', {},
      h('span', {class: 'muted', title: link.predicate}, link.label + ' → '),
      h('a', {href: '#', onclick: (event) => { event.preventDefault(); onFollow(link.otherId); }}, link.other)))),
    h('h3', {}, `Incoming (${links.incoming.length})`),
    h('ul', {class: 'link-list'}, links.incoming.map((link) => h('li', {},
      h('a', {href: '#', onclick: (event) => { event.preventDefault(); onFollow(link.otherId); }}, link.other),
      h('span', {class: 'muted', title: link.predicate}, ' → ' + link.label)))),
  ];
}
