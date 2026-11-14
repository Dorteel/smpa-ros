/**
 * The only place where the frontend talks to the gateway.
 *
 * Concepts: a snapshot is one JSON answer fetched over HTTP. A stream is a
 * WebSocket on which the gateway sends "data" and "status" messages; a data
 * message may be followed by one binary block (a JPEG or packed points).
 * Views never open sockets or build URLs themselves, so a different
 * frontend can be written against the same documented contract.
 */

const BASE = '/api/v1';

/**
 * Build a small DOM tree: an element, its attributes and its children.
 *
 * Attributes starting with "on" become event listeners; "class" and other
 * names become attributes; null or false values are skipped. Children may be
 * nodes or text. This keeps view code free of HTML strings, so data from ROS
 * can never be interpreted as markup.
 */
export function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name.startsWith('on')) element.addEventListener(name.slice(2), value);
    else if (name === 'checked' || name === 'value' || name === 'disabled') element[name] = value;
    else element.setAttribute(name, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return element;
}

/**
 * Fetch one snapshot and return its parsed JSON body.
 *
 * Query parameters are encoded here, because ROS names contain slashes. A
 * response outside the 2xx range becomes an Error carrying the gateway's
 * explanation, so the calling view can show it.
 */
export async function getJSON(path, parameters = {}) {
  const query = new URLSearchParams(parameters).toString();
  const response = await fetch(BASE + path + (query ? '?' + query : ''), {cache: 'no-store'});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Gateway answered ${response.status}`);
  return body;
}

/**
 * Open a stream and keep it open until the returned close function is called.
 *
 * Each data message is delivered to onData together with its binary block
 * when the header announces one. Status messages go to onStatus. If the
 * connection drops (for example because the gateway restarted), onStatus is
 * told the stream is disconnected and a new connection is attempted every
 * two seconds. Calling close stops reconnecting and closes the socket, which
 * makes the gateway unsubscribe from the topic.
 */
export function openStream(path, parameters, {onData, onStatus}) {
  const query = new URLSearchParams(parameters).toString();
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${scheme}://${location.host}${BASE}${path}?${query}`;
  let socket = null;
  let closed = false;
  let retry = null;
  let pendingHeader = null;

  /** Connect once and wire the message handlers for this connection. */
  function connect() {
    socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    socket.onmessage = (event) => {
      if (typeof event.data !== 'string') {
        if (pendingHeader) onData(pendingHeader, event.data);
        pendingHeader = null;
        return;
      }
      const message = JSON.parse(event.data);
      if (message.kind === 'status') onStatus(message);
      else if (message.binary) pendingHeader = message;
      else onData(message, null);
    };
    socket.onclose = () => {
      if (closed) return;
      onStatus({kind: 'status', state: 'disconnected', detail: 'Gateway connection lost; retrying'});
      retry = setTimeout(connect, 2000);
    };
  }

  connect();
  return () => {
    closed = true;
    clearTimeout(retry);
    if (socket) socket.close();
  };
}

/**
 * Call a function now and then repeatedly until the returned stop function is called.
 *
 * The next call is scheduled only after the previous one finished, so a slow
 * gateway is never flooded. Errors are passed to onError and polling goes on.
 */
export function poll(task, intervalMs, onError = () => {}) {
  let stopped = false;
  let timer = null;
  /** Run the task once and schedule the following run. */
  async function run() {
    if (stopped) return;
    try { await task(); } catch (error) { onError(error); }
    if (!stopped) timer = setTimeout(run, intervalMs);
  }
  run();
  return () => { stopped = true; clearTimeout(timer); };
}

/** Format a number with a fixed number of decimals, or a dash when it is missing. */
export function fmt(value, digits = 3) {
  return value === null || value === undefined || Number.isNaN(value) ? '–' : Number(value).toFixed(digits);
}
