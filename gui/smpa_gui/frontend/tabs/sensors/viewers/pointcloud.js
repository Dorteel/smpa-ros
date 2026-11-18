/**
 * Interactive 3D point cloud viewer drawn with WebGL.
 *
 * Concepts: the gateway sends a block of 32-bit floats, three per point
 * (x, y, z) or six per point (x, y, z, r, g, b); the header's stride says
 * which. The camera orbits a target point: yaw and pitch give its direction,
 * distance its range. ROS conventions are used: z points up. Drag rotates,
 * Shift-drag or right-drag pans, the wheel zooms, a double click re-centres.
 */
import {h} from '../../../api.js';
import {fitCanvas} from './canvas.js';

export const streamOptions = {max_points: 20000};

const VERTEX = `attribute vec3 position; attribute vec3 colour; uniform mat4 matrix; uniform float size;
varying vec3 shade; void main() { gl_Position = matrix * vec4(position, 1.0); gl_PointSize = size; shade = colour; }`;
const FRAGMENT = `precision mediump float; varying vec3 shade; void main() { gl_FragColor = vec4(shade, 1.0); }`;

/** Multiply two 4×4 matrices stored column by column. */
function multiply(a, b) {
  const out = new Float32Array(16);
  for (let column = 0; column < 4; column += 1) {
    for (let row = 0; row < 4; row += 1) {
      let sum = 0;
      for (let k = 0; k < 4; k += 1) sum += a[k * 4 + row] * b[column * 4 + k];
      out[column * 4 + row] = sum;
    }
  }
  return out;
}

/** Build a perspective projection for a vertical field of view in radians. */
function perspective(fieldOfView, aspect, near, far) {
  const f = 1 / Math.tan(fieldOfView / 2);
  return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1,
                           0, 0, (2 * far * near) / (near - far), 0]);
}

/** Subtract, cross and normalise small vectors; used to build the camera frame. */
const subtract = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const normalise = (a) => { const n = Math.hypot(...a) || 1; return [a[0] / n, a[1] / n, a[2] / n]; };
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/**
 * Build the view matrix of a camera at `eye` looking at `target` with z up.
 *
 * Also return the camera's right and up directions in world coordinates,
 * which panning needs to move the target parallel to the screen.
 */
function lookAt(eye, target) {
  const forward = normalise(subtract(target, eye));
  const right = normalise(cross(forward, [0, 0, 1]));
  const up = cross(right, forward);
  const matrix = new Float32Array([right[0], up[0], -forward[0], 0, right[1], up[1], -forward[1], 0,
                                   right[2], up[2], -forward[2], 0, -dot(right, eye), -dot(up, eye), dot(forward, eye), 1]);
  return {matrix, right, up};
}

/**
 * Turn the received floats into interleaved position and colour for drawing.
 *
 * Clouds that carry colour are used as they are. Clouds without colour are
 * coloured by height from blue (lowest) through green to red (highest), so
 * their shape is readable.
 */
function withColour(values, stride) {
  if (stride === 6) return values;
  const count = values.length / 3;
  const out = new Float32Array(count * 6);
  let low = Infinity;
  let high = -Infinity;
  for (let i = 0; i < count; i += 1) { low = Math.min(low, values[i * 3 + 2]); high = Math.max(high, values[i * 3 + 2]); }
  const span = high - low || 1;
  for (let i = 0; i < count; i += 1) {
    const t = (values[i * 3 + 2] - low) / span;
    out.set([values[i * 3], values[i * 3 + 1], values[i * 3 + 2],
             Math.max(0, 2 * t - 1), 1 - Math.abs(2 * t - 1), Math.max(0, 1 - 2 * t)], i * 6);
  }
  return out;
}

/** Build the line vertices of a ground grid and the three coordinate axes. */
function guideLines(extent) {
  const lines = [];
  const grey = [0.45, 0.48, 0.52];
  const step = extent / 5;
  for (let i = -5; i <= 5; i += 1) {
    lines.push(i * step, -extent, 0, ...grey, i * step, extent, 0, ...grey);
    lines.push(-extent, i * step, 0, ...grey, extent, i * step, 0, ...grey);
  }
  lines.push(0, 0, 0, 1, 0.2, 0.2, step, 0, 0, 1, 0.2, 0.2, 0, 0, 0, 0.2, 0.8, 0.2, 0, step, 0, 0.2, 0.8, 0.2,
             0, 0, 0, 0.3, 0.5, 1, 0, 0, step, 0.3, 0.5, 1);
  return new Float32Array(lines);
}

/**
 * Create a point cloud viewer inside the given container.
 *
 * If WebGL is not available, the panel says so and ignores data. The first
 * cloud after a reset frames the camera around the points; later clouds
 * keep the user's view. The scene is redrawn only when data or the camera
 * changed.
 */
export function create(container) {
  const canvas = h('canvas', {'aria-label': '3D point cloud', tabindex: 0});
  const caption = h('div', {class: 'panel-foot', style: 'border:0'}, 'Drag: rotate · Shift-drag or right-drag: pan · Wheel: zoom · Double click: re-centre');
  container.append(canvas, caption);
  const gl = canvas.getContext('webgl', {antialias: true});
  if (!gl) {
    container.replaceChildren(h('p', {class: 'notice'}, 'WebGL is not available in this browser, so the 3D view cannot be drawn.'));
    return {update() {}, reset() {}, destroy() {}};
  }
  const program = gl.createProgram();
  for (const [type, source] of [[gl.VERTEX_SHADER, VERTEX], [gl.FRAGMENT_SHADER, FRAGMENT]]) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    gl.attachShader(program, shader);
  }
  gl.linkProgram(program);
  gl.useProgram(program);
  const position = gl.getAttribLocation(program, 'position');
  const colour = gl.getAttribLocation(program, 'colour');
  const pointBuffer = gl.createBuffer();
  const lineBuffer = gl.createBuffer();
  const camera = {target: [0, 0, 0], distance: 5, yaw: -2.2, pitch: 0.5};
  let pointCount = 0;
  let lineCount = 0;
  let framed = false;
  let scheduled = false;
  let latest = null;

  /** Ask for one redraw on the next animation frame, however often it is requested. */
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(draw);
  }

  /** Bind one buffer's interleaved position and colour and draw it with the given primitive. */
  function drawBuffer(buffer, primitive, count) {
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.enableVertexAttribArray(position);
    gl.enableVertexAttribArray(colour);
    gl.vertexAttribPointer(position, 3, gl.FLOAT, false, 24, 0);
    gl.vertexAttribPointer(colour, 3, gl.FLOAT, false, 24, 12);
    gl.drawArrays(primitive, 0, count);
  }

  /** Draw the guide lines and the points from the current camera. */
  function draw() {
    scheduled = false;
    const {width, height, ratio} = fitCanvas(canvas);
    gl.viewport(0, 0, width, height);
    gl.clearColor(0.07, 0.09, 0.12, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    const eye = [camera.target[0] + camera.distance * Math.cos(camera.pitch) * Math.cos(camera.yaw),
                 camera.target[1] + camera.distance * Math.cos(camera.pitch) * Math.sin(camera.yaw),
                 camera.target[2] + camera.distance * Math.sin(camera.pitch)];
    const view = lookAt(eye, camera.target);
    camera.right = view.right;
    camera.up = view.up;
    const matrix = multiply(perspective(0.9, width / height, camera.distance / 100, camera.distance * 100), view.matrix);
    gl.uniformMatrix4fv(gl.getUniformLocation(program, 'matrix'), false, matrix);
    gl.uniform1f(gl.getUniformLocation(program, 'size'), 2.5 * ratio);
    if (lineCount) drawBuffer(lineBuffer, gl.LINES, lineCount);
    if (pointCount) drawBuffer(pointBuffer, gl.POINTS, pointCount);
    canvas.dataset.camera = JSON.stringify({yaw: camera.yaw, pitch: camera.pitch, distance: camera.distance, target: camera.target});
  }

  /**
   * Point the camera at the middle of the cloud from a distance that shows all of it.
   *
   * The guide grid is rebuilt to match the cloud's size.
   */
  function frame(values) {
    const count = values.length / 6;
    if (count === 0) return;
    const centre = [0, 0, 0];
    for (let i = 0; i < count; i += 1) for (let axis = 0; axis < 3; axis += 1) centre[axis] += values[i * 6 + axis] / count;
    let radius = 0;
    for (let i = 0; i < count; i += 1) {
      radius = Math.max(radius, Math.hypot(values[i * 6] - centre[0], values[i * 6 + 1] - centre[1], values[i * 6 + 2] - centre[2]));
    }
    radius = radius || 1;
    camera.target = centre;
    camera.distance = radius * 2.6;
    const lines = guideLines(radius * 1.5);
    gl.bindBuffer(gl.ARRAY_BUFFER, lineBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, lines, gl.STATIC_DRAW);
    lineCount = lines.length / 6;
  }

  let drag = null;
  canvas.addEventListener('pointerdown', (event) => {
    drag = {x: event.clientX, y: event.clientY, pan: event.button !== 0 || event.shiftKey};
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    drag.x = event.clientX;
    drag.y = event.clientY;
    if (drag.pan && camera.right) {
      const scale = camera.distance / canvas.clientHeight;
      for (let axis = 0; axis < 3; axis += 1) camera.target[axis] += (-dx * camera.right[axis] + dy * camera.up[axis]) * scale;
    } else {
      camera.yaw -= dx * 0.008;
      camera.pitch = Math.max(-1.55, Math.min(1.55, camera.pitch + dy * 0.008));
    }
    schedule();
  });
  canvas.addEventListener('pointerup', () => { drag = null; });
  canvas.addEventListener('contextmenu', (event) => event.preventDefault());
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    camera.distance = Math.max(0.01, camera.distance * Math.exp(event.deltaY * 0.0015));
    schedule();
  }, {passive: false});
  canvas.addEventListener('dblclick', () => { if (latest) { frame(latest); schedule(); } });
  const observer = new ResizeObserver(schedule);
  observer.observe(canvas);

  return {
    update(header, binary) {
      latest = withColour(new Float32Array(binary), header.stride);
      pointCount = header.count;
      gl.bindBuffer(gl.ARRAY_BUFFER, pointBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, latest, gl.DYNAMIC_DRAW);
      if (!framed) { frame(latest); framed = pointCount > 0; }
      canvas.dataset.points = String(pointCount);
      caption.textContent = `${header.count} of ${header.total_points} points shown` +
        (header.colour ? ` · colour from ${header.colour}` : ' · coloured by height') + ' · drag, Shift-drag, wheel';
      schedule();
    },
    reset() {
      pointCount = 0;
      framed = false;
      latest = null;
      delete canvas.dataset.points;
      schedule();
    },
    destroy() { observer.disconnect(); },
  };
}
