/**
 * IMU viewer: orientation as a rotating box, plus turn rate and acceleration readouts.
 *
 * Concepts: the orientation quaternion rotates the sensor's own axes into
 * the reference frame. The box and its red (x), green (y) and blue (z) axes
 * are rotated by it and seen from a fixed viewpoint above and behind, with z
 * up. Roll, pitch and yaw are the same orientation expressed as angles
 * about x, y and z. A sensor that reports no orientation gets no box.
 */
import {fmt, h} from '../../../api.js';
import {fitCanvas, themeColour} from './canvas.js';

/** Rotate a vector by a unit quaternion {x, y, z, w}. */
function rotate(q, [x, y, z]) {
  const ix = q.w * x + q.y * z - q.z * y;
  const iy = q.w * y + q.z * x - q.x * z;
  const iz = q.w * z + q.x * y - q.y * x;
  const iw = -q.x * x - q.y * y - q.z * z;
  return [ix * q.w + iw * -q.x + iy * -q.z - iz * -q.y,
          iy * q.w + iw * -q.y + iz * -q.x - ix * -q.z,
          iz * q.w + iw * -q.z + ix * -q.y - iy * -q.x];
}

/** Convert a unit quaternion to roll, pitch and yaw in degrees (rotation about x, y, z). */
export function rollPitchYaw(q) {
  const roll = Math.atan2(2 * (q.w * q.x + q.y * q.z), 1 - 2 * (q.x * q.x + q.y * q.y));
  const pitch = Math.asin(Math.max(-1, Math.min(1, 2 * (q.w * q.y - q.z * q.x))));
  const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
  return [roll, pitch, yaw].map((angle) => angle * 180 / Math.PI);
}

/** Describe a vector's components and length in one line with a unit. */
function vectorText(vector, unit) {
  if ([vector.x, vector.y, vector.z].some((value) => value === null)) return '–';
  return `${fmt(vector.x, 2)}, ${fmt(vector.y, 2)}, ${fmt(vector.z, 2)}  |${fmt(Math.hypot(vector.x, vector.y, vector.z), 2)}| ${unit}`;
}

/**
 * Create an IMU viewer inside the given container.
 *
 * Each message redraws the box and the readouts. Quantities the sensor
 * marks as not provided are labelled so rather than drawn as zeros.
 */
export function create(container) {
  const canvas = h('canvas', {'aria-label': 'IMU orientation', style: 'height:220px'});
  const readout = h('dl', {class: 'readout'});
  container.append(canvas, readout);
  let latest = null;

  /** Project a point of the reference frame onto the canvas from the fixed viewpoint. */
  function project([x, y, z], width, height) {
    const viewYaw = -2.2;
    const viewPitch = 0.45;
    const a = x * Math.cos(viewYaw) + y * Math.sin(viewYaw);
    const b = -x * Math.sin(viewYaw) + y * Math.cos(viewYaw);
    const scale = Math.min(width, height) * 0.3;
    return [width / 2 + b * scale, height / 2 - (z * Math.cos(viewPitch) - a * Math.sin(viewPitch)) * scale];
  }

  /** Draw the box edges and body axes for the newest orientation. */
  function draw() {
    const {width, height, ratio} = fitCanvas(canvas);
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, width, height);
    if (!latest || !latest.has_orientation) {
      context.fillStyle = themeColour('--muted');
      context.font = `${13 * ratio}px sans-serif`;
      context.fillText(latest ? 'This sensor does not report orientation.' : '', 12 * ratio, 24 * ratio);
      return;
    }
    const q = latest.orientation;
    const norm = Math.hypot(q.x, q.y, q.z, q.w);
    const unit = {x: q.x / norm, y: q.y / norm, z: q.z / norm, w: q.w / norm};
    const corners = [];
    for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1]) {
      corners.push(project(rotate(unit, [sx * 0.9, sy * 0.55, sz * 0.2]), width, height));
    }
    context.strokeStyle = themeColour('--muted');
    context.lineWidth = 1.5 * ratio;
    for (const [from, to] of [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]]) {
      context.beginPath(); context.moveTo(...corners[from]); context.lineTo(...corners[to]); context.stroke();
    }
    const origin = project([0, 0, 0], width, height);
    for (const [axis, colour] of [[[1.4, 0, 0], '#e5484d'], [[0, 1.4, 0], '#30a46c'], [[0, 0, 1.4], '#3e63dd']]) {
      context.strokeStyle = colour;
      context.lineWidth = 3 * ratio;
      context.beginPath(); context.moveTo(...origin); context.lineTo(...project(rotate(unit, axis), width, height)); context.stroke();
    }
  }

  const observer = new ResizeObserver(draw);
  observer.observe(canvas);

  return {
    update(header) {
      latest = header;
      draw();
      const angles = header.has_orientation ? rollPitchYaw(header.orientation) : null;
      readout.replaceChildren(
        h('dt', {}, 'Roll, pitch, yaw'), h('dd', {'data-field': 'rpy'}, angles ? angles.map((angle) => `${fmt(angle, 1)}°`).join(', ') : 'not provided'),
        h('dt', {}, 'Angular velocity'), h('dd', {}, header.has_angular_velocity ? vectorText(header.angular_velocity, 'rad/s') : 'not provided'),
        h('dt', {}, 'Linear acceleration'), h('dd', {}, header.has_linear_acceleration ? vectorText(header.linear_acceleration, 'm/s²') : 'not provided'));
    },
    reset() { latest = null; readout.replaceChildren(); draw(); },
    destroy() { observer.disconnect(); },
  };
}
