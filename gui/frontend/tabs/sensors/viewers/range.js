/**
 * Range viewer: one distance reading shown as a number and a position on a scale.
 */
import {fmt, h} from '../../../api.js';

/**
 * Create a range viewer inside the given container.
 *
 * The scale runs from the sensor's minimum to its maximum range. A reading
 * outside those limits, or one that is not a finite number, is reported in
 * words, because such readings conventionally mean "nothing detected" or
 * "too close".
 */
export function create(container) {
  const value = h('div', {class: 'big', 'data-field': 'range'}, '–');
  const marker = h('span', {});
  const bar = h('div', {class: 'bar', style: 'margin:6px 12px 12px'}, marker);
  const details = h('dl', {class: 'readout'});
  container.append(value, bar, details);
  return {
    update(header) {
      const span = (header.max_range ?? 0) - (header.min_range ?? 0);
      const inside = header.range !== null && header.range >= header.min_range && header.range <= header.max_range;
      value.textContent = header.range === null ? 'no reading' : `${fmt(header.range, 3)} m`;
      marker.style.display = inside && span > 0 ? '' : 'none';
      if (inside && span > 0) marker.style.left = `calc(${((header.range - header.min_range) / span) * 100}% - 1px)`;
      details.replaceChildren(
        h('dt', {}, 'Limits'), h('dd', {}, `${fmt(header.min_range, 2)} – ${fmt(header.max_range, 2)} m`),
        h('dt', {}, 'Status'), h('dd', {}, header.range === null ? 'not a finite distance' : inside ? 'within limits' : 'outside sensor limits'),
        h('dt', {}, 'Beam width'), h('dd', {}, header.field_of_view === null ? '–' : `${fmt(header.field_of_view * 180 / Math.PI, 1)}°`),
        h('dt', {}, 'Sensor'), h('dd', {}, header.radiation_type));
    },
    reset() { value.textContent = '–'; marker.style.display = 'none'; details.replaceChildren(); },
    destroy() {},
  };
}
