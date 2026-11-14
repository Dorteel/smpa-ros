/**
 * The SMPA tab: an empty workspace reserved for future SMPA-specific modules.
 *
 * Concepts: an SMPA module will be an object with an id, a title and mount
 * and unmount functions, exactly like a primary tab. Modules are listed in
 * MODULES below. Version 1 deliberately registers none: the conversation and
 * belief views that exist elsewhere in the project are not copied here.
 */
import {h} from '../../api.js';

export const MODULES = [];

export default {
  id: 'smpa',
  title: 'SMPA',

  /**
   * Show the registered modules, or an explanation when there are none.
   *
   * With no modules the workspace states plainly that nothing is installed,
   * so an empty page is not mistaken for a fault.
   */
  mount(container) {
    if (MODULES.length === 0) {
      container.append(h('div', {class: 'empty', id: 'smpa-empty'},
        h('p', {}, 'No SMPA modules are installed yet.'),
        h('p', {}, 'This workspace is reserved for conversation, belief visualization and other ',
          'SMPA views. The ROS 2 and Sensors tabs work without SMPA running.')));
      return;
    }
    for (const module of MODULES) {
      const section = h('section', {class: 'column', 'data-module': module.id});
      container.append(section);
      module.mount(section);
    }
  },

  /** Unmount every registered module so that it stops its own work. */
  unmount() {
    for (const module of MODULES) module.unmount();
  },
};
