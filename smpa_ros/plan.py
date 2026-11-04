"""Select and persist one goal-directed action from the current world model."""
from importlib.resources import files
import json


AVAILABLE_ACTIONS = {
    'look_left': {'endpoint': '/look_left', 'type': 'simulation_actions/action/Look'},
    'look_right': {'endpoint': '/look_right', 'type': 'simulation_actions/action/Look'},
}
ACTION_SCHEMA = {'type': 'object', 'properties': {'action': {'type': 'string',
                 'enum': list(AVAILABLE_ACTIONS)}}, 'required': ['action'], 'additionalProperties': False}



def load_prompt(value, goal='', available_actions=''):
    component = files('smpa_ros')
    prompt = component.joinpath('prompts/plan/default.txt').read_text(encoding='utf-8').format(
        input=value, previous_world_model='', goal=goal, available_actions=available_actions)
    settings = json.loads(component.joinpath('models/plan/default.json').read_text(encoding='utf-8'))
    return prompt, settings


def validate_settings(settings):
    effort = settings.get('reasoning_effort')
    if effort is not None and effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
        raise ValueError('planning.reasoning_effort is unsupported')
    if 'max_output_tokens' in settings:
        limit = settings['max_output_tokens']
        if type(limit) is not int or limit <= 0:
            raise ValueError('planning.max_output_tokens must be a positive integer')


def select_action(directory, cycle, world_model, plan, *, pipeline,
                  plan_identifier=None, model_identifier=None):
    """Generate a strict action decision and persist its inputs, output, and status."""
    destination = directory / 'state' / f'cycle_{cycle:06d}'
    goal_text = (directory / 'goal.md').read_text(encoding='utf-8')
    prompt, settings = load_prompt(world_model, goal=goal_text,
                                    available_actions=json.dumps(AVAILABLE_ACTIONS, indent=2))
    if 'max_output_tokens' in pipeline.get('planning', {}):
        settings['max_new_tokens'] = pipeline['planning']['max_output_tokens']
    if pipeline.get('planning', {}).get('reasoning_effort') is not None:
        settings['reasoning_effort'] = pipeline['planning']['reasoning_effort']
    record = dict(stage='plan', cycle=cycle, input=world_model, goal=goal_text,
                  model_identifier=plan_identifier or model_identifier or 'configured planner',
                  prompt=prompt, available_actions=AVAILABLE_ACTIONS, selected_action=None)
    try:
        if plan is None:
            raise ValueError('Plan stage requires a schema-constrained LLM backend')
        generate = getattr(plan, 'generate', plan)
        text = generate(prompt, **settings, schema=ACTION_SCHEMA)
        if not isinstance(text, str):
            raise ValueError('Planner must return a JSON string')
        record['output'] = text
        decision = json.loads(text)
        if (not isinstance(decision, dict) or set(decision) != {'action'}
                or not isinstance(decision['action'], str) or decision['action'] not in AVAILABLE_ACTIONS):
            raise ValueError('Planner must select exactly one supported action: look_left or look_right')
        record.update(status='generated', selected_action=decision['action'])
    except (Exception, KeyboardInterrupt) as error:
        record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=f'{type(error).__name__}: {error}')
        with (destination / 'plan.json').open('x', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
        raise
    with (destination / 'plan.json').open('x', encoding='utf-8') as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
    return decision['action']
