/**
 * Build a goal editor from an action's Goal schema and check values against it.
 *
 * Concepts: the schema comes from the gateway and describes every field of
 * the Goal message: booleans, integers with their range, floats, strings,
 * arrays (fixed, bounded or unbounded) and nested messages. The editor keeps
 * one plain JavaScript value shaped like the goal and draws one input per
 * field. Checking here gives immediate hints; the gateway checks again and
 * has the final say.
 */
import {h} from '../../api.js';

/** Return the value a field has before the user edits it: false, 0, empty text, or defaults of its parts. */
export function defaultFor(schema) {
  if (schema.kind === 'boolean') return false;
  if (schema.kind === 'integer' || schema.kind === 'float') return 0;
  if (schema.kind === 'string') return '';
  if (schema.kind === 'array') return Array.from({length: schema.fixed_size || 0}, () => defaultFor(schema.item));
  return Object.fromEntries(schema.fields.map((field) => [field.name, defaultFor(field.schema)]));
}

/**
 * Fill in whatever a value leaves out, using the given defaults.
 *
 * Saved configurations and hand-written JSON may omit fields. Missing
 * message fields take their default; everything that is present is kept as
 * it is, even if it is wrong, so that checking can point at it.
 */
export function withDefaults(schema, value, fallback) {
  if (schema.kind !== 'message') {
    if (schema.kind === 'array' && Array.isArray(value)) {
      return value.map((item) => withDefaults(schema.item, item, defaultFor(schema.item)));
    }
    return value === undefined ? fallback : value;
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return value === undefined ? fallback : value;
  const result = {...value};
  for (const field of schema.fields) {
    const base = fallback && fallback[field.name] !== undefined ? fallback[field.name] : defaultFor(field.schema);
    result[field.name] = withDefaults(field.schema, value[field.name], base);
  }
  return result;
}

/**
 * Compare a value with its schema and return every disagreement as {path, message}.
 *
 * The rules are the gateway's: booleans are true or false; integers are
 * whole numbers in the type's range; floats are finite numbers; strings
 * respect their bound; arrays respect their fixed or maximum size; messages
 * contain only known fields. Whole numbers beyond what JavaScript represents
 * exactly are flagged, because they would silently change.
 */
export function validate(schema, value, path = '') {
  const errors = [];
  const fail = (message) => errors.push({path, message});
  if (schema.kind === 'boolean') {
    if (typeof value !== 'boolean') fail('must be true or false');
  } else if (schema.kind === 'integer') {
    if (typeof value !== 'number' || !Number.isInteger(value)) fail(`must be a whole number (${schema.type})`);
    else if (value < schema.minimum || value > schema.maximum) fail(`must be between ${schema.minimum} and ${schema.maximum} (${schema.type})`);
    else if (!Number.isSafeInteger(value)) fail('is too large to be entered exactly in a browser');
  } else if (schema.kind === 'float') {
    if (typeof value !== 'number' || !Number.isFinite(value)) fail(`must be a finite number (${schema.type})`);
    else if (schema.limit && Math.abs(value) > schema.limit) fail(`must fit ${schema.type}`);
  } else if (schema.kind === 'string') {
    if (typeof value !== 'string') fail('must be text');
    else if (schema.max_length !== null && value.length > schema.max_length) fail(`must have at most ${schema.max_length} characters`);
  } else if (schema.kind === 'array') {
    if (!Array.isArray(value)) fail('must be a list');
    else {
      if (schema.fixed_size !== null && value.length !== schema.fixed_size) fail(`must have exactly ${schema.fixed_size} elements`);
      if (schema.max_size !== null && value.length > schema.max_size) fail(`must have at most ${schema.max_size} elements`);
      value.forEach((item, index) => errors.push(...validate(schema.item, item, `${path}[${index}]`)));
    }
  } else if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    fail(`must be an object with the fields of ${schema.type}`);
  } else {
    const known = new Map(schema.fields.map((field) => [field.name, field.schema]));
    for (const [name, item] of Object.entries(value)) {
      const child = path ? `${path}.${name}` : name;
      if (!known.has(name)) errors.push({path: child, message: `is not a field of ${schema.type}`});
      else errors.push(...validate(known.get(name), item, child));
    }
  }
  return errors;
}

/** Describe a field's type briefly for the label next to its input. */
function typeHint(schema) {
  if (schema.kind === 'array') {
    const size = schema.fixed_size !== null ? schema.fixed_size : schema.max_size !== null ? `≤${schema.max_size}` : '';
    return `${typeHint(schema.item)}[${size}]`;
  }
  return schema.type;
}

/**
 * Create an editor for a goal and return its element with get, set and showErrors functions.
 *
 * Every input writes straight into the editor's value and then calls
 * onChange. Adding or removing an array element redraws the form. A number
 * input holding something that is not a number keeps the raw text as the
 * value, so checking reports it instead of silently sending zero.
 * showErrors marks the inputs named by a list of {path, message} entries.
 */
export function createEditor(schema, initial, onChange) {
  let value = structuredClone(initial);
  const host = h('div', {class: 'goal-form'});

  /** Draw the input (or group of inputs) for one field and wire it to its place in the value. */
  function field(itemSchema, current, assign, path) {
    if (itemSchema.kind === 'boolean') {
      return h('input', {type: 'checkbox', checked: current === true, 'data-path': path,
        onchange: (event) => { assign(event.target.checked); onChange(); }});
    }
    if (itemSchema.kind === 'integer' || itemSchema.kind === 'float') {
      return h('input', {type: 'number', value: typeof current === 'number' ? String(current) : '', 'data-path': path,
        step: itemSchema.kind === 'integer' ? '1' : 'any',
        oninput: (event) => {
          const text = event.target.value.trim();
          assign(text !== '' && Number.isFinite(Number(text)) ? Number(text) : text);
          onChange();
        }});
    }
    if (itemSchema.kind === 'string') {
      return h('input', {type: 'text', value: typeof current === 'string' ? current : '', 'data-path': path,
        oninput: (event) => { assign(event.target.value); onChange(); }});
    }
    if (itemSchema.kind === 'array') {
      const items = Array.isArray(current) ? current : [];
      const fixed = itemSchema.fixed_size !== null;
      const full = itemSchema.max_size !== null && items.length >= itemSchema.max_size;
      return h('div', {class: 'goal-array', 'data-path': path},
        items.map((item, index) => h('div', {class: 'goal-array-item'},
          h('span', {class: 'muted'}, `[${index}]`),
          field(itemSchema.item, item, (next) => { items[index] = next; }, `${path}[${index}]`),
          fixed ? null : h('button', {class: 'action', title: 'Remove element', type: 'button',
            onclick: () => { items.splice(index, 1); render(); onChange(); }}, '−'))),
        fixed || full ? null : h('button', {class: 'action', type: 'button', 'data-add': path,
          onclick: () => { if (!Array.isArray(current)) assign(items); items.push(defaultFor(itemSchema.item)); render(); onChange(); }},
          '+ element'));
    }
    const object = current && typeof current === 'object' && !Array.isArray(current) ? current : {};
    return h('div', {class: 'goal-message'}, itemSchema.fields.map((part) => {
      const child = path ? `${path}.${part.name}` : part.name;
      return h('div', {class: 'goal-field' + (part.schema.kind === 'message' || part.schema.kind === 'array' ? ' goal-field-block' : '')},
        h('label', {}, h('span', {class: 'mono'}, part.name), h('span', {class: 'muted'}, typeHint(part.schema))),
        field(part.schema, object[part.name], (next) => { object[part.name] = next; }, child));
    }));
  }

  /** Redraw the whole form from the current value. */
  function render() {
    host.replaceChildren(schema.fields.length
      ? field(schema, value, (next) => { value = next; }, '')
      : h('p', {class: 'muted'}, 'This action\'s goal has no fields.'));
  }

  render();
  return {
    element: host,
    get: () => structuredClone(value),
    set: (next) => { value = structuredClone(next); render(); },
    showErrors: (errors) => {
      for (const input of host.querySelectorAll('[data-path]')) { input.classList.remove('invalid'); input.removeAttribute('title'); }
      for (const error of errors) {
        const input = host.querySelector(`[data-path="${CSS.escape(error.path)}"]`);
        if (input) { input.classList.add('invalid'); input.title = error.message; }
      }
    },
  };
}
