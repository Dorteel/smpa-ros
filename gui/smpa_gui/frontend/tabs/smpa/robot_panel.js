/**
 * The Dialogue's ROS 2 controls: belief context, robot run lifecycle, one cognitive step, and model loading.
 *
 * Concepts: the context says whose beliefs the Dialogue works on. In the
 * independent context only the conversation (left) is used. In the shared
 * context the robot's running run is the belief context, and a cognitive
 * step calls SMPA's Sense → Model → Plan → Act actions on it once.
 *
 * Everything that can make the robot move, or changes what SMPA runs, is
 * done only after a confirmation dialog: starting and stopping a robot run
 * (SMPA's run control), approving the Act of a step (the plan's decision is
 * shown first), and loading a model into SMPA's inference server. Each
 * confirmation sends one request with a new request id; nothing is retried.
 * Changes need the GUI's local session token, so they are unavailable in a
 * read-only GUI or from another computer.
 *
 * Live state (run state, active stage, stage goals, inference state) comes
 * from ROS and is labelled live; it is not persisted history.
 */
import {h, postJSON} from '../../api.js';
import {STAGE_TITLES} from './cognitive_views.js';

const STAGES = ['sense', 'model', 'plan', 'act'];
const STEP_TEXT = {waiting: 'waiting', running: 'running', succeeded: 'done', skipped: 'reused', declined: 'declined',
  aborted: 'aborted', failed: 'failed', rejected: 'rejected', timed_out: 'timed out', unavailable: 'no server',
  interface_missing: 'type missing', pending: 'pending'};

/** Make a request id for one deliberate action. */
function requestId(prefix) {
  return prefix + Array.from(crypto.getRandomValues(new Uint8Array(10)), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * Create the panel and return {element, update, event, destroy}.
 *
 * ``access`` returns {enabled, token}. ``refresh`` asks the Dialogue to
 * read the status again after a change. ``showRun`` selects a run in the
 * SMPA tab's session selector. ``update(ros, dialogue)`` redraws from the
 * latest GET /smpa/ros and GET /smpa/dialogue answers; ``event(message)``
 * takes one Dialogue stream event.
 */

/**
 * Replace an element's children, skipping the null placeholders of optional parts.
 *
 * ``replaceChildren`` would show a null child as the text "null", so absent
 * optional parts (no load in progress, no step yet) are dropped first.
 */
function fill(element, ...children) {
  element.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}
export function createRobotPanel({access, refresh, showRun}) {
  const state = {ros: null, dialogue: null, turn: null, notice: null, live: {}, asked: new Set()};
  const dom = {
    context: h('div', {class: 'segmented', role: 'group', 'aria-label': 'Belief context', id: 'dialogue-context'},
      h('button', {'data-context': 'independent', title: 'The Dialogue keeps its own beliefs through the conversation',
        onclick: () => chooseContext('independent')}, 'Independent'),
      h('button', {'data-context': 'shared', title: 'The Dialogue works on the robot\'s running run',
        onclick: () => chooseContext('shared')}, 'Shared with robot')),
    robot: h('div', {class: 'row live-row', id: 'robot-run', style: 'margin:4px 0'}),
    inference: h('div', {class: 'row live-row', id: 'inference-state', style: 'margin:4px 0'}),
    step: h('div', {id: 'cognitive-step', style: 'margin:4px 0'}),
    notice: h('p', {class: 'muted', id: 'robot-notice', style: 'margin:2px 0;font-size:12.5px'}),
    dialog: h('dialog', {id: 'robot-dialog'}),
  };
  const element = h('details', {class: 'robot-panel', id: 'robot-panel', open: true},
    h('summary', {}, h('strong', {}, 'Robot and inference'), h('span', {class: 'live-badge', style: 'margin-left:8px'}, 'LIVE · ROS 2')),
    h('div', {class: 'row', style: 'margin:4px 0'}, h('span', {class: 'muted'}, 'Beliefs:'), dom.context),
    dom.robot, dom.inference, dom.step, dom.notice, dom.dialog);

  /** Show a short message under the panel, as an error when needed. */
  function say(text, error = false) {
    dom.notice.textContent = text || '';
    dom.notice.className = error ? 'error-text' : 'muted';
  }

  /** Send one change request with the session token; show SMPA's or the gateway's answer. */
  async function send(path, body, success) {
    let answer;
    try {
      answer = await postJSON(path, body, access().token);
    } catch (error) {
      say('The GUI server could not be reached; the outcome is unknown. The status is read again.', true);
      refresh();
      return null;
    }
    if (!answer.ok) say(typeof answer.body.detail === 'string' ? answer.body.detail : `The GUI server answered ${answer.status}`, true);
    else if (success) say(success(answer.body));
    refresh();
    return answer;
  }

  /**
   * Open the confirmation dialog: a title, a warning, details, and two buttons.
   *
   * ``confirm`` runs once when the user confirms; the dialog closes first,
   * so a double click cannot confirm twice. Declining runs ``decline``.
   */
  function ask({id, title, warning, details, confirmText, confirm, decline, input}) {
    let done = false;
    const confirmButton = h('button', {class: 'primary', id: `${id}-confirm`, type: 'button', onclick: () => {
      if (done) return;
      done = true;
      dom.dialog.close();
      confirm(input ? input.value : undefined);
    }}, confirmText);
    fill(dom.dialog, h('h2', {}, title), warning ? h('p', {class: 'warning'}, warning) : null, details || null, input || null,
      h('div', {class: 'row'}, confirmButton, h('button', {class: 'action', id: `${id}-decline`, type: 'button', onclick: () => {
        if (done) return;
        done = true;
        dom.dialog.close();
        if (decline) decline();
      }}, decline ? 'Decline' : 'Cancel')));
    dom.dialog.dataset.kind = id;
    dom.dialog.oncancel = () => { if (!done && decline) { done = true; decline(); } };
    dom.dialog.showModal();
  }

  /** Choose the shared or independent context. */
  function chooseContext(mode) {
    send('/smpa/dialogue/context', {mode}, () => (mode === 'shared'
      ? 'Shared: cognitive steps work on the robot\'s running run.'
      : 'Independent: the conversation keeps its own beliefs; no robot run is used.'));
  }

  /** Ask for a goal and confirm, then ask SMPA's run control to start a robot run. */
  function startRun() {
    const goal = h('textarea', {id: 'robot-goal', rows: 3, style: 'min-height:70px', placeholder: 'Goal for the robot run'});
    ask({id: 'robot-start', title: 'Start a robot run?', confirmText: 'Start run', input: goal,
      warning: 'SMPA\'s experiment senses, plans and acts on its own once it runs. If it controls a robot, the robot may move. '
        + 'Make sure the surroundings are safe and that you can stop it.',
      details: h('p', {class: 'muted'}, 'SMPA\'s run control (StartSmpa) starts its experiment with this goal.'),
      confirm: (text) => {
        if (!text || !text.trim()) { say('A goal is required to start a run.', true); return; }
        send('/smpa/ros/run/start', {goal: text, request_id: requestId('start')},
          (body) => (body.run.accepted ? `Run ${body.run.run_id} started.` : 'SMPA did not start a run (one may already be active).'));
      }});
  }

  /** Confirm, then ask SMPA's run control to stop the active run. */
  function stopRun() {
    ask({id: 'robot-stop', title: 'Stop the robot run?', confirmText: 'Stop run',
      details: h('p', {}, 'SMPA\'s run control (StopSmpa) interrupts its experiment and finalizes the run.'),
      confirm: () => send('/smpa/ros/run/stop', {request_id: requestId('stop')},
        (body) => (body.run.accepted ? 'Stop requested.' : 'SMPA did not accept the stop request.'))});
  }

  /** Choose a registered model and confirm, then ask SMPA's inference server to load it. */
  function loadModel() {
    const listed = (state.ros && state.ros.inference.models.model_ids) || [];
    const choice = h('select', {id: 'model-load-select'}, listed.map((model) => h('option', {value: model}, model)));
    ask({id: 'model-load', title: 'Load a model into SMPA\'s inference server?', confirmText: 'Load model', input: choice,
      details: h('p', {class: 'muted'}, 'The server unloads its current model and loads this one (LoadModel). Loading can take minutes; '
        + 'other inference is refused meanwhile. Model settings stay SMPA\'s ROS parameters.'),
      confirm: (model) => send('/smpa/ros/llm/load', {model_id: model, request_id: requestId('load')}, () => `Loading ${model}…`)});
  }

  /** Start one cognitive step on the robot's run. */
  function runStep() {
    send('/smpa/dialogue/turns', {request_id: requestId('step')}, (body) => {
      state.turn = body.turn;
      draw();
      return 'Cognitive step started.';
    });
  }

  /** Show the plan's decision and ask whether Act may carry it out. */
  function askAct(turn) {
    if (dom.dialog.open || state.asked.has(turn.turn_id)) return;
    state.asked.add(turn.turn_id);
    ask({id: 'act', title: 'Let the robot act?', confirmText: 'Approve Act',
      warning: 'Act executes this decision through SMPA\'s Act stage. If it moves the robot, it moves now. Make sure that is safe.',
      details: h('div', {}, h('p', {class: 'muted'}, `Run ${turn.values.run_id} · plan record ${turn.values.plan_record}`),
        h('pre', {id: 'act-decision'}, JSON.stringify(turn.act_decision, null, 2))),
      confirm: () => send(`/smpa/dialogue/turns/${turn.turn_id}/act`, {approve: true, request_id: requestId('act')}, () => 'Act approved.'),
      decline: () => send(`/smpa/dialogue/turns/${turn.turn_id}/act`, {approve: false, request_id: requestId('act')}, () => 'Act declined; nothing was executed.')});
  }

  /** Draw the robot run line: live state, run id (selectable), active stage, and the start/stop buttons. */
  function drawRobot() {
    const robot = state.ros && state.ros.robot;
    const enabled = access().enabled;
    if (!robot || robot.state !== 'ok') {
      fill(dom.robot, h('span', {class: 'pill pill-off', 'data-state': robot ? robot.state : 'unknown'}, 'run control unavailable'),
        h('span', {class: 'muted', style: 'font-size:12px'}, robot ? robot.detail : 'Reading SMPA\'s status…'));
      return;
    }
    const running = ['running', 'stopping'].includes(robot.run_state);
    fill(dom.robot, 
      h('span', {class: `pill ${running ? 'pill-ok' : ''}`, id: 'robot-run-state', 'data-state': robot.run_state}, `robot run: ${robot.run_state}`),
      robot.run_id ? h('a', {href: '#', class: 'mono', id: 'robot-run-id', 'data-run-id': robot.run_id,
        title: `Run ${robot.run_id}: show it in the session selector`,
        onclick: (event) => { event.preventDefault(); showRun(robot.run_id); }}, robot.run_id.slice(0, 8)) : null,
      robot.active_stage ? h('span', {class: 'muted'}, `· ${robot.active_stage}`) : null,
      h('span', {style: 'flex:1'}),
      h('button', {class: 'action', id: 'robot-start', disabled: !enabled || running, onclick: startRun}, 'Start run…'),
      h('button', {class: 'danger', id: 'robot-stop', disabled: !enabled || robot.run_state !== 'running', onclick: stopRun}, 'Stop run…'));
  }

  /** Draw the inference line: live server state, active model, load progress, and the load button. */
  function drawInference() {
    const inference = state.ros && state.ros.inference;
    const status = inference && inference.status;
    const load = state.ros && state.ros.model_load;
    if (!status || status.state !== 'ok') {
      fill(dom.inference, h('span', {class: 'pill pill-off', id: 'inference-server', 'data-state': status ? status.state : 'unknown'},
        'inference server unavailable'), h('span', {class: 'muted', style: 'font-size:12px'}, status ? status.detail : ''));
      return;
    }
    const style = {ready: 'pill-ok', error: 'pill-off', loading: 'pill-warn', generating: 'pill-warn'}[status.server_state] || '';
    fill(dom.inference, 
      h('span', {class: `pill ${style}`, id: 'inference-server', 'data-state': status.server_state}, `inference: ${status.server_state}`),
      h('span', {class: 'muted mono', id: 'inference-model'}, status.active_model || 'no model'),
      load ? h('span', {class: 'muted', id: 'model-load-state', 'data-state': load.state},
        `· load ${load.model_id}: ${load.state}${load.error ? ` (${load.error})` : ''}`) : null,
      h('span', {style: 'flex:1'}),
      h('button', {class: 'action', id: 'model-load', disabled: !access().enabled || (load && load.state === 'loading'), onclick: loadModel}, 'Load model…'));
    if (status.server_state === 'error') say('SMPA\'s inference server reports an error; conversation replies that update beliefs fail until a model is loaded again.', true);
  }

  /** Draw the context switch, the step button and the progress of the latest step. */
  function drawStep() {
    const dialogue = state.dialogue;
    if (!dialogue) return;
    for (const button of dom.context.children) button.setAttribute('aria-pressed', String(button.dataset.context === dialogue.context.mode));
    const turn = state.turn;
    const robot = state.ros && state.ros.robot;
    const reason = !access().enabled ? 'Changes are disabled in this GUI.'
      : dialogue.steps === 'independent_context' ? 'Choose "Shared with robot" to step the robot\'s run.'
      : dialogue.steps === 'pending_stages' ? `Stages marked pending: ${dialogue.pending_stages.join(', ')}.`
      : !(robot && robot.state === 'ok' && robot.run_state === 'running') && !dialogue.context.run_id ? 'No robot run is running.'
      : dialogue.active_turn ? 'A step is running.' : '';
    fill(dom.step, 
      h('div', {class: 'row', style: 'margin:0'},
        h('button', {class: 'primary', id: 'step-run', disabled: Boolean(reason), onclick: runStep,
          title: 'Call SMPA\'s Sense, Model, Plan and Act actions once on the robot\'s run (Act asks first)'}, 'Run Sense → Model → Plan → Act'),
        h('span', {class: 'muted', id: 'step-reason', style: 'font-size:12px'}, reason)),
      turn ? h('div', {class: 'row step-chips', id: 'step-progress', 'data-state': turn.state, style: 'margin:4px 0'},
        STAGES.map((stage) => {
          const entry = turn.stages[stage];
          const live = state.live[stage];
          return h('span', {class: `pill step-${entry.state}`, 'data-stage': stage, 'data-state': entry.state,
            title: entry.error || ''}, `${STAGE_TITLES[stage]}: ${STEP_TEXT[entry.state] || entry.state}${live ? ' ●' : ''}`);
        }),
        h('span', {class: 'muted', id: 'step-state'}, turn.state === 'awaiting_act' ? 'waiting for your Act decision' : turn.state),
        turn.state === 'failed' && turn.failed_stage && access().enabled
          ? h('button', {class: 'action', id: 'step-retry', onclick: () => send(`/smpa/dialogue/turns/${turn.turn_id}/retry`,
            {request_id: requestId('retry')}, (body) => { state.turn = body.turn; return 'Retrying from the failed stage.'; })},
          `Retry from ${STAGE_TITLES[turn.failed_stage]}`) : null) : null,
      turn && turn.error ? h('p', {class: 'error-text', id: 'step-error', style: 'margin:2px 0'}, turn.error) : null);
    if (turn && turn.state === 'awaiting_act' && access().enabled) askAct(turn);
  }

  /** Redraw everything. */
  function draw() {
    drawRobot();
    drawInference();
    drawStep();
  }

  return {
    element,
    /** Take the latest status answers and redraw; the newest turn is the one the backend lists last. */
    update(ros, dialogue) {
      if (ros) state.ros = ros;
      if (dialogue) {
        state.dialogue = dialogue;
        const latest = dialogue.turns[dialogue.turns.length - 1];
        if (latest && (!state.turn || latest.turn_id === state.turn.turn_id || latest.created_at >= state.turn.created_at)) state.turn = latest;
      }
      draw();
    },
    /** Take one stream event: live stage activity, or a change that needs a fresh status. */
    event(message) {
      if (message.event === 'stage_status' && message.message) {
        state.live[message.message.stage] = message.message.active;
        drawStep();
        return message.message;
      }
      if (message.turn_id || message.event === 'model_load') refresh();
      return null;
    },
    /** Close an open dialog when the panel goes away. */
    destroy() { if (dom.dialog.open) dom.dialog.close(); },
  };
}
