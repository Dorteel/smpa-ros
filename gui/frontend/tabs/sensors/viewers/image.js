/**
 * Camera viewer for raw and compressed image topics.
 *
 * Concepts: the gateway sends each frame as a JPEG block with a header that
 * describes the original image. Depth-like images arrive contrast-stretched
 * ("scaled"), which the caption states, because brightness then no longer
 * means a fixed distance.
 */
import {h} from '../../../api.js';

/**
 * Create an image viewer inside the given container.
 *
 * update shows the newest frame and releases the previous one, so memory
 * does not grow. reset removes the picture when the topic changes. destroy
 * releases the last frame.
 */
export function create(container) {
  const picture = h('img', {alt: 'Live camera frame'});
  const caption = h('div', {class: 'panel-foot', style: 'border:0'}, '');
  container.append(picture, caption);
  let address = null;

  /** Release the object URL of the frame that is no longer shown. */
  function release() {
    if (address) URL.revokeObjectURL(address);
    address = null;
  }

  return {
    update(header, binary) {
      release();
      address = URL.createObjectURL(new Blob([binary], {type: 'image/jpeg'}));
      picture.src = address;
      picture.dataset.frames = String(Number(picture.dataset.frames || 0) + 1);
      const size = header.width ? `${header.width}×${header.height}` : '';
      caption.textContent = [size, header.encoding || header.format, header.scaled ? 'contrast-stretched' : '']
        .filter(Boolean).join(' · ');
    },
    reset() {
      release();
      picture.removeAttribute('src');
      delete picture.dataset.frames;
      caption.textContent = '';
    },
    destroy() { release(); },
  };
}
