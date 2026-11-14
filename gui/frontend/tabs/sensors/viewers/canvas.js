/**
 * Canvas helpers shared by the sensor viewers.
 */

/**
 * Match a canvas's pixel size to its displayed size and return its dimensions.
 *
 * A canvas is drawn at its own pixel resolution and then stretched to its
 * CSS size. Setting the resolution from the displayed size, multiplied by
 * the screen's pixel ratio, keeps drawings sharp. The returned width and
 * height are in canvas pixels, with the ratio used.
 */
export function fitCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return {width, height, ratio};
}

/** Read a colour defined by the page's theme, such as "--text" or "--muted". */
export function themeColour(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim() || '#888';
}

/**
 * Choose a round step (1, 2 or 5 times a power of ten) so that about `count` steps span `extent`.
 *
 * Used for range rings and scale bars, so labels are round numbers.
 */
export function niceStep(extent, count = 5) {
  const rough = Math.max(extent, 1e-9) / count;
  const power = Math.pow(10, Math.floor(Math.log10(rough)));
  const fraction = rough / power;
  return (fraction < 1.5 ? 1 : fraction < 3.5 ? 2 : fraction < 7.5 ? 5 : 10) * power;
}
