"""Execute ROS actions and persist their results before advancing the cycle."""
from importlib.resources import files
import json
from pathlib import Path

import yaml

from smpa_ros.backends.base import stage_generation, generation_arguments

from smpa_ros.plan import AVAILABLE_ACTIONS, enabled_actions, validate_decision


def load_prompt(value, *, prompt='default', configuration=None):
    component = files('smpa_ros')
    if not prompt or '/' in prompt or '\\' in prompt or prompt in ('.', '..'):
        raise ValueError('Prompt must be a filename stem')
    selected = (configuration or {}).get('stage_prompts', {}).get('act', {})
    template = (selected['text'] if prompt == selected.get('name') else
                component.joinpath(f'prompts/act/{prompt}.txt').read_text(encoding='utf-8'))
    prompt = template.format(
        input=value, previous_world_model='', goal='', available_actions='')
    if configuration is None:
        from ament_index_python.packages import get_package_share_directory
        defaults = yaml.safe_load(
            (Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text(encoding='utf-8'))
        configuration = dict(defaults, **(configuration or {}))
    settings = stage_generation(configuration, 'act')
    return prompt, settings


def perform_action(directory, cycle, action, act, *, prompt_name='default',
                   record_name='act.json', provenance=None, configuration=None):
    """Execute the selected action and record success, failure, or interruption."""
    destination = directory / 'state' / f'cycle_{cycle:06d}'
    record = dict(stage='act', cycle=cycle, requested_action=action,
                  endpoint=AVAILABLE_ACTIONS.get(action, {}).get('endpoint'), goal={}, goal_accepted=None,
                  result_status=None, success=False)
    record.update(provenance or {})
    try:
        record['prompt'], record['generation_parameters'] = load_prompt(action, prompt=prompt_name, configuration=configuration)
        validate_decision(dict(action=action, **record.get('parameters', {})), enabled_actions(configuration))
        if act is None:
            raise ValueError('Act stage requires a ROS action executor')
        act(action, record)
        if (record['goal_accepted'] is not True or record['result_status'] != 4
                or record['success'] is not True):
            raise RuntimeError('Action did not complete successfully')
        record['status'] = 'succeeded'
    except (Exception, KeyboardInterrupt) as error:
        record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      success=False, error=f'{type(error).__name__}: {error}')
        with (destination / record_name).open('x', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
        raise
    with (destination / record_name).open('x', encoding='utf-8') as output:
        json.dump(record, output, ensure_ascii=False, indent=2)


def execute_action(node, executor, action, record, timeout, *, speech=None, transcript='', configuration=None):
    """Send a registered movement goal and retain cancellation/result checks."""
    from action_msgs.msg import GoalStatus
    from rclpy.action import ActionClient
    from rosidl_runtime_py.convert import message_to_ordereddict
    if action not in enabled_actions(configuration) or AVAILABLE_ACTIONS.get(action, {}).get('type') is None:
        raise ValueError(f'Unsupported action: {action}')
    try:
        from importlib import import_module
        kind = getattr(import_module('simulation_actions.action'), AVAILABLE_ACTIONS[action]['type'].rsplit('/', 1)[1])
    except (ImportError, AttributeError) as error:
        raise RuntimeError(f"{AVAILABLE_ACTIONS[action]['type']} is unavailable; run ./install.sh to rebuild "
                           'the current Webots interfaces and source the workspace installation') from error
    client = ActionClient(node, kind, AVAILABLE_ACTIONS[action]['endpoint'])
    pending = handle = None
    result_future = None
    terminal = False

    def wait(future, phase):
        executor.spin_until_future_complete(future, timeout_sec=timeout)
        if not future.done():
            raise TimeoutError(f'{action}: timeout waiting for {phase}')
        return future.result()

    try:
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError(f'{action}: action server unavailable')
        # Look/Turn use angle zero for the server default; LookAhead has an empty goal.
        goal = kind.Goal()
        record['goal'] = dict(message_to_ordereddict(goal))
        record['feedback'] = []

        def feedback(message):
            record['feedback'].append(dict(message_to_ordereddict(message.feedback)))

        node.get_logger().info(f"Sending {action}: {record['goal']}")
        pending = client.send_goal_async(goal, feedback_callback=feedback)
        handle = wait(pending, 'goal acceptance')
        record['goal_accepted'] = handle.accepted
        if not handle.accepted:
            raise RuntimeError(f'{action}: goal rejected')
        result_future = handle.get_result_async()
        response = wait(result_future, 'action result')
        terminal = True
        record.update(result_status=response.status,
                      result=dict(message_to_ordereddict(response.result)),
                      success=(response.status == GoalStatus.STATUS_SUCCEEDED and response.result.success))
        node.get_logger().info(f"{action}: ROS status {response.status}; result {record['result']}")
        if not record['success']:
            raise RuntimeError(f'{action}: unsuccessful ROS goal status/result {response.status}: {record["result"]}')
    finally:
        try:
            if pending is not None and not terminal:
                try:
                    if handle is None:
                        handle = wait(pending, 'late goal acceptance before cancellation')
                        record['goal_accepted'] = handle.accepted
                    if handle.accepted:
                        canceled = wait(handle.cancel_goal_async(), 'cancellation response')
                        record['cancel_requested'] = bool(canceled.goals_canceling)
                        if result_future is None:
                            result_future = handle.get_result_async()
                        response = wait(result_future, 'terminal result after cancellation')
                        record['cancellation_result_status'] = response.status
                except Exception as error:
                    record['cancellation_error'] = str(error)
                    node.get_logger().error(f'{action}: could not confirm goal termination: {error}')
        finally:
            client.destroy()
    if speech is not None:
        from smpa_ros_interfaces.action import Talk
        speech.perform(speech.talker, Talk.Goal(text=f'I heard {transcript}'))
