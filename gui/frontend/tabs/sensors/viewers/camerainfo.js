/**
 * Camera calibration viewer: intrinsic parameters and the calibration matrices.
 */
import {fmt, h} from '../../../api.js';

/** Draw a row-major list of numbers as a table with the given number of columns. */
function matrix(values, columns) {
  const rows = [];
  for (let start = 0; start < values.length; start += columns) {
    rows.push(h('tr', {}, values.slice(start, start + columns).map((value) => h('td', {}, fmt(value, 4)))));
  }
  return h('table', {class: 'matrix', style: 'width:auto;margin:0 12px 8px'}, h('tbody', {}, rows));
}

/**
 * Create a calibration viewer inside the given container.
 *
 * The focal lengths and principal point are read from the intrinsic matrix
 * K. The fields of view follow from the focal lengths and the image size
 * and are omitted when a focal length is zero (an uncalibrated camera).
 */
export function create(container) {
  const host = h('div', {style: 'overflow:auto;max-height:460px'});
  container.append(host);
  return {
    update(header) {
      const [fx, , cx, , fy, cy] = header.k;
      const view = (size, focal) => (focal ? `${fmt(2 * Math.atan(size / (2 * focal)) * 180 / Math.PI, 1)}°` : '–');
      host.replaceChildren(h('dl', {class: 'readout'},
        h('dt', {}, 'Image size'), h('dd', {'data-field': 'size'}, `${header.width} × ${header.height}`),
        h('dt', {}, 'Focal length'), h('dd', {'data-field': 'focal'}, `fx ${fmt(fx, 2)}, fy ${fmt(fy, 2)} px`),
        h('dt', {}, 'Principal point'), h('dd', {}, `cx ${fmt(cx, 2)}, cy ${fmt(cy, 2)} px`),
        h('dt', {}, 'Field of view'), h('dd', {}, `${view(header.width, fx)} horizontal, ${view(header.height, fy)} vertical`),
        h('dt', {}, 'Distortion'), h('dd', {}, `${header.distortion_model || 'none'}  [${header.d.map((value) => fmt(value, 4)).join(', ')}]`),
        h('dt', {}, 'Binning'), h('dd', {}, `${header.binning_x} × ${header.binning_y}`),
        h('dt', {}, 'Region of interest'), h('dd', {}, header.roi.width ? `${header.roi.width} × ${header.roi.height} at (${header.roi.x_offset}, ${header.roi.y_offset})` : 'full image')),
        h('h3', {style: 'margin:6px 12px'}, 'K (intrinsics)'), matrix(header.k, 3),
        h('h3', {style: 'margin:6px 12px'}, 'R (rectification)'), matrix(header.r, 3),
        h('h3', {style: 'margin:6px 12px'}, 'P (projection)'), matrix(header.p, 4));
    },
    reset() { host.replaceChildren(); },
    destroy() {},
  };
}
