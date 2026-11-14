/**
 * Joint state viewer: a table of joints with position, velocity and effort.
 */
import {fmt, h} from '../../../api.js';

/**
 * Create a joint state viewer inside the given container.
 *
 * Each joint gets one row. The position is also drawn as a marker on a
 * scale from -π to π, which suits revolute joints; prismatic joints beyond
 * that span are clamped to the end of the scale while the number stays
 * exact. Values a message does not report are shown as dashes.
 */
export function create(container) {
  const body = h('tbody', {});
  container.append(h('div', {style: 'overflow:auto;max-height:420px'}, h('table', {},
    h('thead', {}, h('tr', {}, h('th', {}, 'Joint'), h('th', {}, 'Position'), h('th', {}, ''), h('th', {}, 'Velocity'), h('th', {}, 'Effort'))), body)));
  return {
    update(header) {
      body.replaceChildren(...header.names.map((name, index) => {
        const position = header.position[index];
        const marker = h('span', {});
        if (position === null || position === undefined) marker.style.display = 'none';
        else marker.style.left = `calc(${(Math.max(-Math.PI, Math.min(Math.PI, position)) / (2 * Math.PI) + 0.5) * 100}% - 1px)`;
        return h('tr', {'data-joint': name}, h('td', {class: 'mono'}, name), h('td', {class: 'mono'}, fmt(position, 4)),
          h('td', {}, h('div', {class: 'bar'}, marker)), h('td', {class: 'mono'}, fmt(header.velocity[index], 4)),
          h('td', {class: 'mono'}, fmt(header.effort[index], 4)));
      }));
    },
    reset() { body.replaceChildren(); },
    destroy() {},
  };
}
