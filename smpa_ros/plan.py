"""Select and persist one goal-directed action from the current world model."""
from importlib.resources import files
import json
from pathlib import Path

import yaml

from smpa_ros.backends.base import stage_generation, generation_arguments


AVAILABLE_ACTIONS = {
    'look_left': {'label': 'Look left', 'group': 'Physical actions', 'endpoint': '/look_left', 'type': 'simulation_actions/action/Look'},
    'look_right': {'label': 'Look right', 'group': 'Physical actions', 'endpoint': '/look_right', 'type': 'simulation_actions/action/Look'},
    'look_ahead': {'label': 'Look ahead', 'group': 'Physical actions', 'endpoint': '/look_ahead', 'type': 'simulation_actions/action/LookAhead'},
    'turn_left': {'label': 'Turn left', 'group': 'Physical actions', 'endpoint': '/turn_left', 'type': 'simulation_actions/action/Turn'},
    'turn_right': {'label': 'Turn right', 'group': 'Physical actions', 'endpoint': '/turn_right', 'type': 'simulation_actions/action/Turn'},
    'observe': {'label': 'Observe', 'group': 'Epistemic actions', 'endpoint': None, 'type': None},
}
DEFAULT_ACTIONS = ('look_left', 'look_right')
CANDIDATE_PROPERTIES = ('color', 'size', 'shape', 'material', 'texture', 'pattern',
                        'orientation', 'condition', 'state', 'parts', 'spatial relations')


def enabled_actions(configuration=None):
    """One whitelist for prompts, constrained decoding, and execution."""
    names = (configuration or {}).get('enabled_actions', list(DEFAULT_ACTIONS))
    if (not isinstance(names, list) or not names or any(not isinstance(name, str) or
            name not in AVAILABLE_ACTIONS for name in names) or len(set(names)) != len(names)):
        raise ValueError('enabled_actions must be a nonempty list of unique registered action names')
    return {name: AVAILABLE_ACTIONS[name] for name in names}


def refinement_settings(configuration):
    from copy import deepcopy
    settings = deepcopy(configuration.get('scene_refinement', {}))
    if not isinstance(settings, dict) or set(settings) - {'enabled', 'properties'}:
        raise ValueError('scene_refinement accepts enabled and properties only')
    settings.setdefault('enabled', False)
    settings.setdefault('properties', list(CANDIDATE_PROPERTIES))
    properties = settings['properties']
    if (type(settings['enabled']) is not bool or not isinstance(properties, list) or not properties
            or any(not isinstance(item, str) or not item.strip() for item in properties)
            or len({item.strip().casefold() for item in properties}) != len(properties)):
        raise ValueError('scene_refinement requires a boolean enabled and unique nonempty properties')
    if settings['enabled'] and 'observe' not in enabled_actions(configuration):
        raise ValueError('Scene refinement requires Observe to be enabled')
    return settings


def action_schema(actions):
    schema = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': list(actions)}},
              'required': ['action'], 'additionalProperties': False}
    if 'observe' in actions:
        # Nullable fields keep the schema compatible with strict API providers.
        schema['properties'].update(target={'type': ['string', 'null']}, question={'type': ['string', 'null']})
        schema['required'] += ['target', 'question']
    return schema


ACTION_SCHEMA = action_schema(DEFAULT_ACTIONS)


def validate_decision(decision, actions):
    if not isinstance(decision, dict) or not isinstance(decision.get('action'), str) or decision['action'] not in actions:
        raise ValueError('Planner must select exactly one supported action from the enabled whitelist')
    if decision['action'] == 'observe':
        if (set(decision) - {'action', 'target', 'question'} or not isinstance(decision.get('question'), str)
                or not decision['question'].strip() or decision.get('target') is not None and
                (not isinstance(decision['target'], str) or not decision['target'].strip())):
            raise ValueError('Observe requires a nonempty question and an optional target')
    elif set(decision) - {'action', 'target', 'question'} or decision.get('target') is not None or decision.get('question') is not None:
        raise ValueError('Movement actions do not accept observation parameters')


def load_prompt(value, goal='', available_actions='', *, prompt='default', configuration=None):
    component = files('smpa_ros')
    if not prompt or '/' in prompt or '\\' in prompt or prompt in ('.', '..'):
        raise ValueError('Prompt must be a filename stem')
    selected = (configuration or {}).get('stage_prompts', {}).get('plan', {})
    template = (selected['text'] if prompt == selected.get('name') else
                component.joinpath(f'prompts/plan/{prompt}.txt').read_text(encoding='utf-8'))
    prompt = template.format(
        input=value, previous_world_model='', goal=goal, available_actions=available_actions)
    if configuration is None:
        from ament_index_python.packages import get_package_share_directory
        defaults = yaml.safe_load(
            (Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text(encoding='utf-8'))
        configuration = dict(defaults, **(configuration or {}))
    settings = stage_generation(configuration, 'plan')
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
                  plan_identifier=None, model_identifier=None, prompt_name='default',
                  record_name='plan.json', provenance=None):
    """Generate a strict action decision and persist its inputs, output, and status."""
    destination = directory / 'state' / f'cycle_{cycle:06d}'
    goal_text = (directory / 'goal.md').read_text(encoding='utf-8')
    record = dict(stage='plan', cycle=cycle, input=world_model, goal=goal_text,
                  model_identifier=plan_identifier or model_identifier or 'configured planner',
                  prompt=None, available_actions=enabled_actions(pipeline), selected_action=None)
    record.update(provenance or {})
    try:
        prompt, settings = load_prompt(world_model, goal=goal_text,
                                        available_actions=json.dumps({key: {'description': entry['label']} for key, entry in enabled_actions(pipeline).items()}, indent=2), prompt=prompt_name, configuration=pipeline)
        if 'observe' in enabled_actions(pipeline):
            prompt += '\nFor Observe, provide a question and optional target (null for the whole scene). For movement, target and question must be null.'
        pending = pipeline.get('_pending_question')
        if pending is not None:
            settings['output_schema'] = action_schema(['observe'])
            settings['output_schema']['properties']['target'] = {'type': 'string', 'enum': [pending['target']]}
            settings['output_schema']['properties']['question'] = {'type': 'string', 'enum': [pending['question']]}
            record['available_actions'] = {'observe': AVAILABLE_ACTIONS['observe']}
            prompt += '\nObserve exactly this unattempted question: ' + json.dumps(pending, ensure_ascii=False)
        record['generation_parameters'] = settings
        record['prompt'] = prompt
        if plan is None:
            raise ValueError('Plan stage requires a schema-constrained LLM backend')
        generate = getattr(plan, 'generate', plan)
        text = generate(prompt, **generation_arguments(settings))
        if not isinstance(text, str):
            raise ValueError('Planner must return a JSON string')
        record['output'] = text
        decision = json.loads(text)
        validate_decision(decision, enabled_actions(pipeline))
        from jsonschema import validate
        validate(decision, settings['output_schema'])
        record['parameters'] = {key: value for key, value in decision.items() if key != 'action'}
        record.update(status='generated', selected_action=decision['action'])
    except (Exception, KeyboardInterrupt) as error:
        record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error=f'{type(error).__name__}: {error}')
        with (destination / record_name).open('x', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
        raise
    with (destination / record_name).open('x', encoding='utf-8') as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
    return decision['action']
