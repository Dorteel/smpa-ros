/**
 * 2D laser scan viewer: a top-down drawing of the scan around the sensor.
 *
 * Concepts: ray i has the angle angle_min + i * angle_increment, measured
 * counter-clockwise from the sensor's forward (x) axis. The drawing puts
 * forward at the top and left at the left, as seen from above. Rays without
 * a finite range, or outside the sensor's limits, are not drawn.
 */
import {h} from '../../../api.js';
import {fitCanvas, niceStep, themeColour} from './canvas.js';

/**
 * Create a laser scan viewer inside the given container.
 *
 * The view first shows the sensor's whole maximum range. The mouse wheel
 * zooms in and out around the sensor. Range rings with round distances give
 * the scale.
 */
export function create(container) {
  const canvas = h('canvas', {'aria-label': 'Laser scan seen from above'});
  const caption = h('div', {class: 'panel-foot', style: 'border:0'}, 'Forward is up · Wheel: zoom');
  container.append(canvas, caption);
  let scan = null;
  let zoom = 1;

  /** Draw rings, axes, the sensor and one dot per valid ray. */
  function draw() {
    const {width, height, ratio} = fitCanvas(canvas);
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, width, height);
    if (!scan) return;
    const finiteMax = scan.ranges.reduce((most, value) => (value !== null && value <= scan.range_max ? Math.max(most, value) : most), 0);
    const extent = (Number.isFinite(scan.range_max) && scan.range_max > 0 ? scan.range_max : finiteMax || 1) / zoom;
    const scale = (Math.min(width, height) / 2 - 12 * ratio) / extent;
    const cx = width / 2;
    const cy = height / 2;
    context.strokeStyle = themeColour('--line');
    context.fillStyle = themeColour('--muted');
    context.lineWidth = ratio;
    context.font = `${11 * ratio}px sans-serif`;
    const step = niceStep(extent, 4);
    for (let radius = step; radius <= extent * 1.01; radius += step) {
      context.beginPath();
      context.arc(cx, cy, radius * scale, 0, Math.PI * 2);
      context.stroke();
      context.fillText(`${+radius.toFixed(2)} m`, cx + 4 * ratio, cy - radius * scale - 3 * ratio);
    }
    context.beginPath();
    context.moveTo(cx, cy - extent * scale); context.lineTo(cx, cy + extent * scale);
    context.moveTo(cx - extent * scale, cy); context.lineTo(cx + extent * scale, cy);
    context.stroke();
    context.fillStyle = themeColour('--accent');
    let drawn = 0;
    scan.ranges.forEach((range, index) => {
      if (range === null || range < scan.range_min || range > scan.range_max) return;
      const angle = scan.angle_min + index * scan.angle_increment;
      const x = cx - Math.sin(angle) * range * scale;
      const y = cy - Math.cos(angle) * range * scale;
      context.fillRect(x - ratio, y - ratio, 2 * ratio, 2 * ratio);
      drawn += 1;
    });
    context.fillStyle = themeColour('--bad');
    context.beginPath();
    context.moveTo(cx, cy - 7 * ratio); context.lineTo(cx - 5 * ratio, cy + 5 * ratio); context.lineTo(cx + 5 * ratio, cy + 5 * ratio);
    context.fill();
    canvas.dataset.points = String(drawn);
    caption.textContent = `${drawn} of ${scan.total_rays} rays drawn · limits ${+scan.range_min.toFixed(3)}–${+scan.range_max.toFixed(3)} m · forward is up · wheel zooms`;
  }

  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    zoom = Math.max(0.2, Math.min(50, zoom * Math.exp(-event.deltaY * 0.0015)));
    canvas.dataset.zoom = String(zoom);
    draw();
  }, {passive: false});
  const observer = new ResizeObserver(draw);
  observer.observe(canvas);

  return {
    update(header) { scan = header; draw(); },
    reset() { scan = null; zoom = 1; delete canvas.dataset.points; draw(); },
    destroy() { observer.disconnect(); },
  };
}
