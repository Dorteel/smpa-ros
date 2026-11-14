/**
 * Satellite position viewer: the current fix and its recent track on a map.
 *
 * Concepts: positions are projected with the Web Mercator projection used
 * by common web maps. By default the map is a plain coordinate grid that
 * needs no internet. The user may switch on OpenStreetMap background tiles;
 * these are fetched by the browser from tile.openstreetmap.org and therefore
 * need an internet connection and reveal the viewed area to that service,
 * which is why they are off until requested.
 */
import {fmt, h} from '../../../api.js';
import {fitCanvas, niceStep, themeColour} from './canvas.js';

const TILE = 256;
const STATUS = {'-1': 'no fix', 0: 'fix', 1: 'fix with satellite augmentation', 2: 'fix with ground augmentation'};

/** Convert latitude and longitude in degrees to Web Mercator pixels at a zoom level. */
export function mercator(latitude, longitude, zoom) {
  const size = TILE * Math.pow(2, zoom);
  const sine = Math.sin(latitude * Math.PI / 180);
  return [(longitude + 180) / 360 * size, (0.5 - Math.log((1 + sine) / (1 - sine)) / (4 * Math.PI)) * size];
}

/**
 * Create a satellite position viewer inside the given container.
 *
 * Valid fixes are appended to a track of at most 500 points and the map is
 * centred on the newest one. Invalid fixes update the readout only. The
 * wheel changes the zoom level. Background tiles, when enabled, are drawn
 * under the track as they finish loading; tiles that fail to load are
 * simply left out.
 */
export function create(container) {
  const canvas = h('canvas', {'aria-label': 'Position track'});
  const readout = h('dl', {class: 'readout'});
  const tilesToggle = h('input', {type: 'checkbox'});
  const controls = h('div', {class: 'panel-foot', style: 'border:0'},
    h('label', {}, tilesToggle, ' OpenStreetMap background (needs internet)'), h('span', {}, '© OpenStreetMap contributors when enabled'));
  container.append(canvas, controls, readout);
  const track = [];
  const tiles = new Map();
  let zoom = 17;

  /** Return a loaded tile image, or start loading it and return null for now. */
  function tile(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (!tiles.has(key)) {
      const picture = new Image();
      picture.crossOrigin = 'anonymous';
      picture.onload = draw;
      picture.src = `https://tile.openstreetmap.org/${key}.png`;
      tiles.set(key, picture);
    }
    const picture = tiles.get(key);
    return picture.complete && picture.naturalWidth ? picture : null;
  }

  /** Draw the background (tiles or grid), the track, the newest position and a scale bar. */
  function draw() {
    const {width, height, ratio} = fitCanvas(canvas);
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, width, height);
    if (track.length === 0) return;
    const newest = track[track.length - 1];
    const [centreX, centreY] = mercator(newest.latitude, newest.longitude, zoom);
    const toCanvas = ([x, y]) => [width / 2 + (x - centreX) * ratio, height / 2 + (y - centreY) * ratio];
    if (tilesToggle.checked) {
      const span = Math.pow(2, zoom);
      const halfW = width / ratio / 2;
      const halfH = height / ratio / 2;
      for (let tx = Math.floor((centreX - halfW) / TILE); tx <= Math.floor((centreX + halfW) / TILE); tx += 1) {
        for (let ty = Math.floor((centreY - halfH) / TILE); ty <= Math.floor((centreY + halfH) / TILE); ty += 1) {
          if (ty < 0 || ty >= span) continue;
          const picture = tile(zoom, ((tx % span) + span) % span, ty);
          const [left, top] = toCanvas([tx * TILE, ty * TILE]);
          if (picture) context.drawImage(picture, left, top, TILE * ratio, TILE * ratio);
        }
      }
    } else {
      context.strokeStyle = themeColour('--line');
      context.lineWidth = ratio;
      for (let offset = -width; offset <= width; offset += 64 * ratio) {
        context.beginPath(); context.moveTo(width / 2 + offset, 0); context.lineTo(width / 2 + offset, height); context.stroke();
        context.beginPath(); context.moveTo(0, height / 2 + offset); context.lineTo(width, height / 2 + offset); context.stroke();
      }
    }
    context.strokeStyle = themeColour('--accent');
    context.lineWidth = 2 * ratio;
    context.beginPath();
    track.forEach((fix, index) => {
      const [x, y] = toCanvas(mercator(fix.latitude, fix.longitude, zoom));
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.stroke();
    context.fillStyle = themeColour('--bad');
    context.beginPath(); context.arc(width / 2, height / 2, 5 * ratio, 0, Math.PI * 2); context.fill();
    const metresPerPixel = 156543.03392 * Math.cos(newest.latitude * Math.PI / 180) / Math.pow(2, zoom);
    const barMetres = niceStep(metresPerPixel * 120, 1);
    const barPixels = barMetres / metresPerPixel * ratio;
    context.fillStyle = themeColour('--text');
    context.fillRect(12 * ratio, height - 14 * ratio, barPixels, 3 * ratio);
    context.font = `${11 * ratio}px sans-serif`;
    context.fillText(barMetres >= 1000 ? `${barMetres / 1000} km` : `${barMetres} m`, 12 * ratio, height - 18 * ratio);
    canvas.dataset.fixes = String(track.length);
  }

  tilesToggle.onchange = draw;
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    zoom = Math.max(2, Math.min(19, zoom + (event.deltaY < 0 ? 1 : -1)));
    draw();
  }, {passive: false});
  const observer = new ResizeObserver(draw);
  observer.observe(canvas);

  return {
    update(header) {
      if (header.valid) {
        const last = track[track.length - 1];
        if (!last || last.latitude !== header.latitude || last.longitude !== header.longitude) {
          track.push({latitude: header.latitude, longitude: header.longitude});
          if (track.length > 500) track.shift();
        }
        draw();
      }
      readout.replaceChildren(
        h('dt', {}, 'Latitude, longitude'), h('dd', {'data-field': 'position'}, `${fmt(header.latitude, 6)}°, ${fmt(header.longitude, 6)}°`),
        h('dt', {}, 'Altitude'), h('dd', {}, header.altitude === null ? '–' : `${fmt(header.altitude, 1)} m`),
        h('dt', {}, 'Status'), h('dd', {}, STATUS[header.status] || `status ${header.status}`),
        h('dt', {}, 'Horizontal accuracy'), h('dd', {}, header.horizontal_accuracy_m === null ? 'unknown' : `± ${fmt(header.horizontal_accuracy_m, 1)} m`));
    },
    reset() { track.length = 0; readout.replaceChildren(); delete canvas.dataset.fixes; draw(); },
    destroy() { observer.disconnect(); },
  };
}
