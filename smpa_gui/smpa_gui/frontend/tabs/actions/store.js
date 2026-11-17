/**
 * Saved goal configurations, kept in this browser's local storage.
 *
 * Concepts: a configuration is a named set of goal values for one action
 * type. Configurations are stored per action type, so they can be reused
 * with any server of that type. They are data only: loading one fills the
 * editor and never sends anything.
 */

const KEY = 'smpa-gui.goal-configurations.v1';

/**
 * Read all saved configurations as {actionType: {name: goalValues}}.
 *
 * Storage that is unavailable or holds something unreadable counts as empty,
 * so the Actions tab works in private windows and after manual tampering.
 */
function readAll(storage) {
  try {
    const parsed = JSON.parse(storage.getItem(KEY) || '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (error) {
    return {};
  }
}

/** List the names of the configurations saved for an action type, alphabetically. */
export function configurationNames(type, storage = window.localStorage) {
  return Object.keys(readAll(storage)[type] || {}).sort();
}

/** Return the goal values saved under a name for an action type, or null. */
export function loadConfiguration(type, name, storage = window.localStorage) {
  const found = (readAll(storage)[type] || {})[name];
  return found === undefined ? null : found;
}

/**
 * Save goal values under a name for an action type, replacing an earlier entry of that name.
 *
 * Return true when the values were stored and false when the browser
 * refused (for example because storage is disabled or full).
 */
export function saveConfiguration(type, name, values, storage = window.localStorage) {
  const all = readAll(storage);
  all[type] = {...(all[type] || {}), [name]: values};
  try {
    storage.setItem(KEY, JSON.stringify(all));
    return true;
  } catch (error) {
    return false;
  }
}

/** Remove one saved configuration; other configurations are untouched. */
export function deleteConfiguration(type, name, storage = window.localStorage) {
  const all = readAll(storage);
  if (all[type]) delete all[type][name];
  try { storage.setItem(KEY, JSON.stringify(all)); } catch (error) { /* nothing to undo */ }
}
