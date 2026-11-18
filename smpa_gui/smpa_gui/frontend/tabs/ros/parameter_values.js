/**
 * Turn what the user typed into a ROS parameter value and check it against the parameter's descriptor.
 *
 * Concepts: a parameter has a type (bool, integer, double, string, or an
 * array of one of these, or a byte array) and a descriptor (read-only,
 * dynamic typing, integer or floating-point range with a step). A proposal
 * is a new type and value for one parameter. These rules are the gateway's
 * own (ros_parameters.check_change); checking here gives immediate hints,
 * the gateway checks again, and the node has the final word.
 */

export const SETTABLE_TYPES = ['bool', 'integer', 'double', 'string', 'byte_array', 'bool_array', 'integer_array',
  'double_array', 'string_array'];
// JSON cannot carry NaN or infinities, so the gateway sends them as these names (API.md, "Non-finite numbers").
export const NON_FINITE = {nan: NaN, inf: Infinity, '-inf': -Infinity};
const INTEGER_LOW = -(2n ** 63n);
const INTEGER_HIGH = 2n ** 63n - 1n;

/** Return the element type of an array type ("integer_array" -> "integer", "byte_array" -> "byte"), or null. */
export function elementType(type) {
  if (type === 'byte_array') return 'byte';
  return type.endsWith('_array') ? type.slice(0, -'_array'.length) : null;
}

/**
 * Tell whether a value received for a double (or a double array element) is a non-finite number sent by name.
 *
 * Only doubles can be NaN or infinite, so the names count only for the
 * types double and double_array; a string parameter whose text is "nan"
 * is ordinary text.
 */
export function isNonFinite(type, value) {
  return (type === 'double' || type === 'double_array') && typeof value === 'string'
    && Object.prototype.hasOwnProperty.call(NON_FINITE, value);
}

/**
 * Show a current value for reading: names for non-finite doubles, JSON for strings and lists, plain text otherwise.
 *
 * A NaN or infinite double is shown as its name without quotes (nan, inf,
 * -inf), also inside a double array, so it is not mistaken for text. A
 * missing value is shown as a dash.
 */
export function displayValue(type, value) {
  if (value === null || value === undefined) return '–';
  if (isNonFinite(type, value)) return value;
  if (type === 'double_array' && Array.isArray(value)) {
    return '[' + value.map((item) => (isNonFinite(type, item) ? item : JSON.stringify(item))).join(',') + ']';
  }
  return typeof value === 'object' || typeof value === 'string' ? JSON.stringify(value) : String(value);
}

/**
 * Show a value the way it is edited: arrays and strings as JSON, numbers and booleans as plain text.
 *
 * Arrays are edited as JSON lists, so a string array keeps its quotes and
 * an element containing a comma stays one element. A non-finite double
 * cannot be set from here, so its edit field starts empty and the user
 * must type a finite number; in a double array its name stays visible as a
 * quoted element, which the check reports until it is replaced. Either way
 * the name of a non-finite number is never sent as a value.
 */
export function editText(type, value) {
  if (value === null || value === undefined || isNonFinite(type, value)) return '';
  if (elementType(type) || Array.isArray(value)) return JSON.stringify(value);
  return String(value);
}

/**
 * Parse the text of an input into a value of the given type, or report why it cannot be parsed.
 *
 * Booleans accept "true" and "false". Integers must be written as whole
 * numbers; doubles as finite numbers. Strings are taken as they are.
 * Arrays must be written as a JSON list. Returns {value} or {error}. The
 * returned value is not yet checked against the descriptor; see check().
 */
export function parseInput(type, text) {
  if (type === 'string') return {value: text};
  if (type === 'bool') {
    if (text === 'true' || text === 'false') return {value: text === 'true'};
    return {error: 'must be true or false'};
  }
  if (type === 'integer' || type === 'double') {
    const trimmed = text.trim();
    if (trimmed === '' || !Number.isFinite(Number(trimmed))) return {error: 'must be a number'};
    if (type === 'integer' && !/^[+-]?\d+$/.test(trimmed)) return {error: 'must be a whole number'};
    return {value: Number(trimmed)};
  }
  try {
    const value = JSON.parse(text);
    if (!Array.isArray(value)) return {error: 'must be a JSON list such as [1, 2, 3]'};
    return {value};
  } catch (error) {
    return {error: 'must be a JSON list such as [1, 2, 3]'};
  }
}

/** Check one element value against an element type and return the problem, or null. */
function scalarProblem(kind, value) {
  if (kind === 'bool') return typeof value === 'boolean' ? null : 'must be true or false';
  if (kind === 'integer' || kind === 'byte') {
    if (typeof value !== 'number' || !Number.isInteger(value)) return 'must be a whole number';
    if (kind === 'byte') return value >= 0 && value <= 255 ? null : 'must be between 0 and 255';
    if (!Number.isSafeInteger(value)) return BigInt(value) < INTEGER_LOW || BigInt(value) > INTEGER_HIGH
      ? 'must fit a 64-bit integer' : 'is too large to be entered exactly in a browser';
    return null;
  }
  if (kind === 'double') return typeof value === 'number' && Number.isFinite(value) ? null : 'must be a finite number';
  if (kind === 'string') return typeof value === 'string' ? null : 'must be text';
  return `has unsupported type ${kind}`;
}

/** Turn a range bound into a number: a finite number stays, the names nan, inf and -inf become NaN and ±Infinity. */
function bound(value) {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(NON_FINITE, value) ? NON_FINITE[value] : value;
}

/**
 * Tell whether a number lies within a ROS parameter range and on its step grid.
 *
 * Both ends are included; an end may be infinite (sent as "inf" or
 * "-inf"). A step of zero allows any value; otherwise the value must be a
 * whole number of steps above the lower end, or equal the upper end.
 * Floating-point steps are compared with a small tolerance.
 */
export function inRange(value, range, integer) {
  const from = bound(range.from);
  const to = bound(range.to);
  const step = bound(range.step);
  if (value < from || value > to) return false;
  if (!step || value === to) return true;
  const distance = (value - from) / step;
  return integer ? Number.isInteger(distance) : Math.abs(distance - Math.round(distance)) < 1e-9;
}

/**
 * Check a proposed type and value for a parameter and return every problem as text.
 *
 * The order of the rules follows the gateway: read-only parameters cannot
 * change; the type may change only with dynamic typing; every element must
 * fit the type; integer and floating-point ranges with their steps must be
 * respected. An empty list means the proposal may be sent.
 */
export function check(parameter, type, value) {
  const problems = [];
  if (parameter.read_only) return ['is read-only; the node does not allow changing it'];
  if (!SETTABLE_TYPES.includes(type)) return [`has unsupported type ${type}`];
  if (type !== parameter.type && parameter.type !== 'not_set' && parameter.dynamic_typing !== true) {
    problems.push(`must stay of type ${parameter.type} (the node does not allow changing the type)`);
  }
  const element = elementType(type);
  if (element) {
    if (!Array.isArray(value)) problems.push('must be a list');
    else value.forEach((item, index) => { const problem = scalarProblem(element, item); if (problem) problems.push(`[${index}] ${problem}`); });
  } else {
    const problem = scalarProblem(type, value);
    if (problem) problems.push(problem);
  }
  if (problems.length) return problems;
  if (type === 'integer' && parameter.integer_range && !inRange(value, parameter.integer_range, true)) {
    const range = parameter.integer_range;
    problems.push(`must be within ${range.from}…${range.to}` + (range.step ? ` in steps of ${range.step}` : ''));
  }
  if (type === 'double' && parameter.floating_point_range && !inRange(value, parameter.floating_point_range, false)) {
    const range = parameter.floating_point_range;
    problems.push(`must be within ${range.from}…${range.to}` + (range.step ? ` in steps of ${range.step}` : ''));
  }
  return problems;
}

/** Describe a parameter's constraints in one short line: read-only, dynamic typing, range and free text. */
export function constraintText(parameter) {
  const parts = [];
  if (parameter.read_only) parts.push('read-only');
  if (parameter.dynamic_typing) parts.push('type may change');
  const range = parameter.integer_range || parameter.floating_point_range;
  if (range) parts.push(`${range.from}…${range.to}` + (range.step ? ` step ${range.step}` : ''));
  if (parameter.additional_constraints) parts.push(parameter.additional_constraints);
  if (parameter.descriptor_known === false) parts.push('no descriptor (not checked here)');
  return parts.join(' · ');
}
