/**
 * The Workspace's central canvas: an empty drawing surface with zoom, pan, reset, fit and a grid.
 *
 * Concepts: the canvas is a Cytoscape.js surface with no elements. It is
 * reserved for the future visual ROS 2 editor, which will place nodes and
 * interfaces on it; today nothing is ever added, so it stays empty.
 *
 * The viewport is the visible part of the canvas: a zoom factor and a pan
 * offset (the screen position, in pixels, of the canvas origin). Reset shows
 * the origin in the middle at 100 %. Fit-to-view frames the canvas content;
 * while the canvas is empty it frames the sheet, a fixed area around the
 * origin where the editor will start placing things.
 *
 * The grid is drawn as a CSS background of the canvas element that follows
 * the viewport, so it costs nothing when the viewport does not change. The
 * viewport is published on the element as data attributes (data-zoom,
 * data-pan-x, data-pan-y, data-elements), so tests and other views can read
 * it without touching Cytoscape objects.
 */
import {h} from '../../api.js';

export const SHEET = {x: -600, y: -400, width: 1200, height: 800};
const MIN_ZOOM = 0.1;
const MAX_ZOOM = 4;
const GRID_STEP = 24;

/**
 * Keep a zoom factor within the range the canvas allows.
 *
 * Returns the factor itself when it is inside the range, otherwise the
 * nearest limit.
 */
function clampZoom(zoom) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/**
 * Create the canvas inside a host element and return the controls the Workspace needs.
 *
 * The host must already be part of the page, so the canvas has a size.
 * ``initial`` is a viewport saved earlier ({zoom, pan: {x, y}}) or null;
 * with null the canvas starts reset. ``showGrid`` decides whether the grid
 * is drawn. ``onChange`` is told the viewport after every zoom or pan, so
 * the Workspace can remember it.
 *
 * The returned object offers: reset, fit, zoomBy (about the middle of the
 * canvas), setGrid, viewport, elementCount, resize and destroy. Nothing in
 * it adds elements; user panning and wheel zooming are on, node dragging and
 * box selection are off, because there is nothing to edit yet.
 */
export function createCanvas(host, {initial = null, showGrid = true, onChange = () => {}} = {}) {
  const element = h('div', {id: 'workspace-canvas', class: 'ws-canvas', tabindex: '0', role: 'application',
    'aria-label': 'Empty workspace canvas. Drag to pan, scroll or press plus and minus to zoom, 0 to reset, F to fit.'});
  host.append(element);
  const cy = window.cytoscape({container: element, elements: [], minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM,
    boxSelectionEnabled: false, autoungrabify: true, userPanningEnabled: true, userZoomingEnabled: true,
    style: []});
  let grid = showGrid;

  /** Return the canvas size in screen pixels. */
  function size() {
    return {width: element.clientWidth, height: element.clientHeight};
  }

  /**
   * Draw the grid for the current viewport and publish the viewport on the element.
   *
   * The grid cell is GRID_STEP canvas units, so it grows and shrinks with the
   * zoom; when cells would become smaller than eight pixels, every fifth line
   * is drawn instead, so a zoomed-out canvas does not turn grey. The
   * background is shifted by the pan offset, so the lines stay attached to
   * the canvas while it is dragged.
   */
  function publish() {
    const zoom = cy.zoom();
    const pan = cy.pan();
    let cell = GRID_STEP * zoom;
    while (cell < 8) cell *= 5;
    element.style.setProperty('--grid-cell', `${cell}px`);
    element.style.setProperty('--grid-x', `${pan.x}px`);
    element.style.setProperty('--grid-y', `${pan.y}px`);
    element.classList.toggle('ws-grid', grid);
    element.dataset.zoom = zoom.toFixed(4);
    element.dataset.panX = pan.x.toFixed(1);
    element.dataset.panY = pan.y.toFixed(1);
    element.dataset.elements = String(cy.elements().length);
    element.dataset.grid = String(grid);
  }

  /** Report a changed viewport: redraw the grid and tell the Workspace. */
  function changed() {
    publish();
    onChange(viewport());
  }

  /** Return the current viewport as plain numbers. */
  function viewport() {
    return {zoom: cy.zoom(), pan: {...cy.pan()}};
  }

  /** Show the canvas origin in the middle of the canvas at 100 % zoom. */
  function reset() {
    const {width, height} = size();
    cy.viewport({zoom: 1, pan: {x: width / 2, y: height / 2}});
  }

  /**
   * Fit the canvas content into view, or the sheet while the canvas is empty.
   *
   * With elements present, Cytoscape fits their bounding box with a margin.
   * Without elements the sheet is used instead: the zoom is the largest that
   * shows the whole sheet with a margin, and the sheet's centre (the origin)
   * is put in the middle of the canvas.
   */
  function fit() {
    if (cy.elements().length) { cy.fit(undefined, 40); return; }
    const {width, height} = size();
    const zoom = clampZoom(Math.min((width - 80) / SHEET.width, (height - 80) / SHEET.height));
    const centre = {x: SHEET.x + SHEET.width / 2, y: SHEET.y + SHEET.height / 2};
    cy.viewport({zoom, pan: {x: width / 2 - centre.x * zoom, y: height / 2 - centre.y * zoom}});
  }

  /** Zoom in (factor above 1) or out (below 1) about the middle of the canvas. */
  function zoomBy(factor) {
    const {width, height} = size();
    cy.zoom({level: clampZoom(cy.zoom() * factor), renderedPosition: {x: width / 2, y: height / 2}});
  }

  /** Switch the grid on or off without changing the viewport. */
  function setGrid(on) {
    grid = on;
    publish();
  }

  /**
   * Handle the canvas keyboard shortcuts while the canvas has the focus.
   *
   * Plus and minus zoom, 0 resets and F fits; arrow keys pan by 40 pixels.
   * Other keys are left alone so the page keeps working normally.
   */
  function onKey(event) {
    const pans = {ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40]};
    if (event.key === '+' || event.key === '=') zoomBy(1.25);
    else if (event.key === '-') zoomBy(0.8);
    else if (event.key === '0') reset();
    else if (event.key === 'f' || event.key === 'F') fit();
    else if (pans[event.key]) cy.panBy({x: pans[event.key][0], y: pans[event.key][1]});
    else return;
    event.preventDefault();
  }

  element.addEventListener('keydown', onKey);
  cy.on('viewport', changed);
  if (initial && Number.isFinite(initial.zoom) && initial.pan) {
    cy.viewport({zoom: clampZoom(initial.zoom), pan: {x: initial.pan.x, y: initial.pan.y}});
  } else {
    reset();
  }
  publish();

  return {
    element, reset, fit, zoomBy, setGrid, viewport,
    /** Tell how many elements the canvas holds; the Workspace never adds any, so this is 0. */
    elementCount: () => cy.elements().length,
    /** Let Cytoscape pick up a new canvas size; the zoom and pan stay as they are. */
    resize: () => { cy.resize(); publish(); },
    /** Remove the canvas and release Cytoscape. */
    destroy: () => { element.removeEventListener('keydown', onKey); cy.destroy(); element.remove(); },
  };
}
