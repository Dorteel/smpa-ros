#!/usr/bin/env python3
"""Local editor for config.yaml; launch the existing ROS workflow unchanged."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlsplit

import yaml
from jsonschema.validators import validator_for

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smpa_ros.plan import (ACTION_SCHEMA, AVAILABLE_ACTIONS, enabled_actions, action_schema,
                           refinement_settings)
from smpa_ros.model import local_model_info, resolve_vision_model, unify_model
from smpa_ros.backends.base import (GENERATION_KEYS, stage_generation, generation_capabilities,
                                    validate_generation, validate_action_schema)

STAGES = ('sense', 'model', 'plan', 'act')


def credential_values(config=None):
    # Inspect .env without loading credentials into GUI configuration or logs.
    values = dict(os.environ)
    path = ROOT / '.env'
    if path.is_file():
        from dotenv import dotenv_values
        values = {**dotenv_values(path), **values}
    if config is None:
        config = yaml.safe_load((ROOT / 'config.yaml').read_text())
    key_names = {entry['api_key_env'] for entry in config.values()
                 if isinstance(entry, dict) and isinstance(entry.get('api_key_env'), str)}
    return [value for key, value in values.items() if value and
            (key in key_names or any(word in key.upper() for word in ('API_KEY', 'PASSWORD', 'SECRET', 'TOKEN', 'CREDENTIAL')))]



def model_choices(config, stage):
    """Discover compatible local directories; retain registry aliases and API choices."""
    choices = {}
    root = ROOT / 'models'
    if stage == 'act':
        return choices
    vision = stage == 'sense'
    directory = root / ('vlm' if vision else 'llm')
    for path in sorted(directory.iterdir()) if directory.is_dir() else []:
        if not path.is_dir() or path.name.startswith('.'):
            continue
        info = local_model_info(path, vision=vision, model_root=root)
        # Recognizable models for other stages are unavailable here, not loadable.
        label = f"[{info['status']}] local: {path.name} ({info['model_id']})"
        choices[label] = dict(info, backend='transformers', model=path.name)
    if vision:
        entries = {}
        for path in sorted(root.glob('*.yaml')):
            entry = yaml.safe_load(path.read_text())
            if isinstance(entry, dict) and 'name' in entry and 'model_id' in entry:
                entries[entry['name']] = entry
    else:
        entries = yaml.safe_load((root / 'model_library.yaml').read_text())['models']
    for name, entry in entries.items():
        backend = entry.get('backend', 'transformers')
        if backend == 'ollama' and not vision:
            choices[f'[Ollama: availability unchecked] {name}'] = dict(backend=backend, model=name)
            continue
        if backend != 'transformers':
            continue
        match = next((choice for choice in choices.values() if
                      choice['model_id'] == entry['model_id'] or
                      entry.get('path') and choice['path'] == str((root / entry['path']).resolve())), None)
        if match is not None:
            match['model'] = name
            if 'enable_thinking' in entry:
                match['enable_thinking'] = entry['enable_thinking']
            continue
        if vision:
            try:
                selected = resolve_vision_model(dict(model=name, vision={}), root, root)
                info = local_model_info(selected['path'], vision=True, model_id=entry['model_id'], model_root=root)
            except (OSError, ValueError, KeyError, TypeError) as error:
                info = dict(model_id=entry['model_id'], status='unavailable', reason=str(error))
        else:
            info = local_model_info(root / entry['path'], model_id=entry['model_id'], model_root=root)
        choices[f"[{info['status']}] local: {name} ({entry['model_id']})"] = dict(
            info, backend='transformers', model=name,
            **({'enable_thinking':entry['enable_thinking']} if 'enable_thinking' in entry else {}))
    modality = 'vision' if vision else 'text'
    for provider, entry in config.items():
        if isinstance(entry, dict) and 'base_url' in entry and modality in entry.get('models', {}):
            identifier = entry['models'][modality]
            choices[f'{provider}: {identifier} [API: availability unchecked]'] = dict(
                backend='api', provider=provider, model_id=identifier)
    return choices


def selected_model_label(config, stage, choices):
    section = config['model'] if isinstance(config.get('model'), dict) else config.get('vision' if stage == 'sense' else 'modeling', {})
    if stage == 'plan':
        section = dict(section, **config.get('planning', {}))
    for label, choice in choices.items():
        if choice['backend'] != section.get('backend', 'transformers'):
            continue
        if choice['backend'] == 'api':
            if choice.get('provider') == section.get('provider', 'openai'):
                return label
        elif section.get('path'):
            directory = Path(section['path']).expanduser()
            if not directory.is_absolute():
                directory = (ROOT if directory.parts[0] == 'models' else ROOT / 'models') / directory
            if choice.get('path') == str(directory.resolve()):
                return label
        elif choice.get('model') == (config.get('model') if stage == 'sense' else section.get('model')):
            return label
    return ''


def validate(config):
    """Validate only settings implemented by the existing stage backends."""
    if not isinstance(config, dict):
        raise ValueError('Configuration must be a YAML mapping')
    secrets = credential_values(config)

    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key).lower()
                if (any(word in name for word in ('api_key', 'password', 'secret', 'authorization', 'access_token', 'credential'))
                        or name in ('token', 'key', 'api_token', 'bearer_token')) and not name.endswith('_env'):
                    raise ValueError('Keep credentials in .env or environment variables')
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif isinstance(value, str):
            if any(secret in value for secret in secrets):
                raise ValueError('Credential data cannot be saved')
            if value.startswith(('https://', 'http://')):
                parsed = urlsplit(value)
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError('Provider URLs must not contain credentials, queries, or fragments')
    check(config)
    enabled_actions(config)
    experiment = refinement_settings(config)
    if experiment['enabled'] and 'json_schema' not in stage_capabilities(config, 'sense')['response_formats']:
        raise ValueError('Scene refinement requires JSON Schema support from the shared model')
    execution = config.get('execution', {})
    cycles = execution.get('max_cycles', 1)
    if type(cycles) is not int or cycles < 1:
        raise ValueError('execution.max_cycles must be a positive integer')
    timeout = execution.get('action_timeout', 30.0)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('action_timeout must be finite and positive')
    for stage in STAGES:
        selected = config['stage_prompts'][stage]
        name, text = selected['name'], selected['text']
        if not name or Path(name).name != name or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('Prompt names must be filename stems')
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'{stage}: prompt must not be empty')
        if stage != 'sense':
            try:
                text.format(input='', previous_world_model='', goal='', available_actions='')
            except (KeyError, ValueError, IndexError, AttributeError):
                raise ValueError(f'{stage}: invalid prompt placeholders; escape literal braces as {{{{ and }}}}') from None
        settings = stage_generation(config, stage)
        if settings['response_format'] == 'json_object' and 'json' not in text.lower():
            raise ValueError(f'{stage}: JSON object mode requires an explicit JSON instruction in the prompt')
        section = config['model'] if isinstance(config.get('model'), dict) else config.get('vision' if stage == 'sense' else 'modeling', {})
        if stage == 'plan':
            section = dict(section, **config.get('planning', {}))
        if stage != 'act' and section.get('backend', 'transformers') == 'transformers' and section.get('path'):
            directory = Path(section['path']).expanduser()
            if not directory.is_absolute():
                directory = (ROOT if directory.parts[0] == 'models' else ROOT / 'models') / directory
            info = local_model_info(directory, vision=stage == 'sense' or isinstance(config.get('model'), dict),
                                    model_id=section.get('model_id'), model_root=ROOT / 'models')
            if info['status'] != 'complete':
                raise ValueError(f"{stage}: {info['status']}: {info['reason']}")
        capabilities = stage_capabilities(config, stage)
        validate_generation(settings, backend='act' if stage == 'act' else None, capabilities=None if stage == 'act' else capabilities)
        vision = config['model'] if isinstance(config.get('model'), dict) else config.get('vision', {})
        if stage == 'sense' and vision.get('backend') != 'api' and vision.get('device') == 'cpu' and vision.get('dtype') == 'float16':
            raise ValueError('CPU float16 is unsupported; use auto or float32')
    validate_action_schema(stage_generation(config, 'plan')['output_schema'])


def stage_capabilities(config, stage, choice=None):
    if stage == 'act':
        return generation_capabilities('act')
    section = config['model'] if isinstance(config.get('model'), dict) else config.get('vision' if stage == 'sense' else 'modeling', {})
    if stage == 'plan':
        section = dict(section, **config.get('planning', {}))
    section = dict(section, **(choice or {}))
    backend = section.get('backend')
    if backend is None:
        registry = yaml.safe_load((ROOT / 'models/model_library.yaml').read_text())['models']
        backend = 'transformers' if stage == 'sense' else registry.get(section.get('model'), {}).get('backend', 'transformers')
    provider = config.get(section.get('provider', 'openai'), {})
    return generation_capabilities(backend, vision=stage == 'sense' or isinstance(config.get('model'), dict), provider=provider,
        model_id=section.get('model_id', provider.get('models', {}).get('vision' if stage == 'sense' else 'text')))


def save_configuration(candidate, path):
    """Validate before atomically replacing the one editable configuration file."""
    from tempfile import NamedTemporaryFile
    validate(candidate)
    path = Path(path)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.config-', delete=False) as output:
        pending = Path(output.name)
        try:
            yaml.safe_dump(candidate, output, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def load_config():
    config = yaml.safe_load((ROOT / 'config.yaml').read_text())
    for stage in STAGES:
        name = config.get('prompt', 'describe_scene') if stage == 'sense' else 'default'
        config.setdefault('stage_prompts', {}).setdefault(stage, dict(
            name=name, text=(ROOT / 'smpa_ros' / 'prompts' / stage / f'{name}.txt').read_text()))
    effective = {stage: stage_generation(config, stage) for stage in STAGES}
    config['stage_settings'] = effective
    for section in ('vision', 'modeling', 'planning'):
        for key in (*GENERATION_KEYS, 'output_schema'):
            config.get(section, {}).pop(key, None)
    return unify_model(config)


def main():
    config = load_config()
    if '--check' in sys.argv:
        validate(config)
        print('Configuration, prompts, model registry, and Plan schema loaded and validated.')
        print(f"Shared vision/text model: {len(model_choices(config, 'sense'))} discovered choices")
        return
    root = tk.Tk()
    root.title('SMPA Run Configurator')
    root.geometry('1120x850')
    root.minsize(850, 650)
    global_panel = ttk.Frame(root, padding=12)
    global_panel.pack(fill='x')
    ttk.Label(global_panel, text='Shared model — Sense / Model / Plan').pack(side='left')
    choices = model_choices(config, 'sense')
    model = tk.StringVar(value=selected_model_label(config, 'sense', choices))
    dropdown = ttk.Combobox(global_panel, textvariable=model, state='readonly', values=list(choices), width=70)
    dropdown.pack(side='left', fill='x', expand=True, padx=12)
    ttk.Label(global_panel, text='Cycles').pack(side='left')
    cycles = tk.StringVar(value=str(config.get('execution', {}).get('max_cycles', 1)))
    cycle_input = ttk.Spinbox(global_panel, from_=1, to=sys.maxsize, width=7, textvariable=cycles,
        validate='key', validatecommand=(root.register(lambda value: value == '' or
            value.isascii() and value.isdecimal() and int(value) >= 1), '%P'))
    cycle_input.pack(side='left', padx=(8, 0))
    action_variables = {name: tk.BooleanVar(value=name in enabled_actions(config)) for name in AVAILABLE_ACTIONS}
    experiment_panel = ttk.Frame(root, padding=(12, 0, 12, 0))
    experiment_panel.pack(fill='x')
    experiment = tk.BooleanVar(value=refinement_settings(config)['enabled'])
    properties = tk.StringVar(value=', '.join(refinement_settings(config)['properties']))
    ttk.Checkbutton(experiment_panel, text='Frozen-image scene refinement', variable=experiment,
                    command=lambda: experiment_changed()).pack(side='left')
    ttk.Label(experiment_panel, text='Candidate properties').pack(side='left', padx=8)
    ttk.Entry(experiment_panel, textvariable=properties).pack(side='left', fill='x', expand=True)
    notebook = ttk.Notebook(root)
    notebook.pack(fill='both', expand=True, padx=12, pady=12)
    widgets = {}
    process = None
    events = queue.Queue()
    secret_values = credential_values()

    def replace(editor, value):
        editor.delete('1.0', 'end')
        editor.insert('1.0', value)

    def read(editor):
        return editor.get('1.0', 'end-1c')

    def format_changed(stage):
        ui = widgets[stage]
        ui['schema'].configure(state='normal' if ui['variables']['response_format'].get() == 'json_schema' else 'disabled')

    def actions_changed():
        if 'plan' in widgets:
            editor = widgets['plan']['schema']
            editor.configure(state='normal')
            replace(editor, json.dumps(action_schema([name for name, value in action_variables.items() if value.get()]), indent=2))

    def experiment_changed():
        if experiment.get():
            action_variables['observe'].set(True)
        actions_changed()

    def populate(stage):
        ui = widgets[stage]
        values = stage_generation(config, stage)
        capabilities = stage_capabilities(config, stage, choices.get(model.get()))
        for key, variable in ui['variables'].items():
            value = values.get(key)
            variable.set('' if value is None else str(value))
            enabled = key in capabilities['parameters'] or key == 'response_format' and bool(capabilities['response_formats'])
            ui['fields'][key].configure(state=('readonly' if key in ('reasoning_effort', 'response_format') else 'normal') if enabled else 'disabled')
        ui['fields']['reasoning_effort']['values'] = ['', *capabilities['reasoning_efforts']]
        ui['fields']['response_format']['values'] = ['json_schema'] if stage == 'plan' else capabilities['response_formats']
        replace(ui['schema'], json.dumps(values.get('output_schema', ACTION_SCHEMA if stage == 'plan' else {'type': 'object'}), indent=2)) if str(ui['schema']['state']) != 'disabled' else None
        ui['extras'].configure(state='normal' if stage == 'act' or stage == 'sense' and choices.get(model.get(), {}).get('backend') == 'transformers' else 'disabled')
        format_changed(stage)

    def model_changed(stage):
        if stage == 'act':
            return
        # An explicit model selection updates unavailable controls visibly.
        ui = widgets[stage]
        capabilities = stage_capabilities(config, stage, choices.get(model.get()))
        changed = []
        for key in ('temperature', 'max_output_tokens', 'reasoning_effort', 'seed'):
            supported = key in capabilities['parameters']
            if not supported and ui['variables'][key].get():
                changed.append(key)
                ui['variables'][key].set('')
            ui['fields'][key].configure(state=('readonly' if key == 'reasoning_effort' else 'normal') if supported else 'disabled')
        ui['fields']['reasoning_effort']['values'] = ['', *capabilities['reasoning_efforts']]
        ui['fields']['response_format']['values'] = ['json_schema'] if stage == 'plan' else capabilities['response_formats']
        if ui['variables']['response_format'].get() not in ui['fields']['response_format']['values']:
            ui['variables']['response_format'].set('json_schema' if stage == 'plan' else 'text')
            changed.append('response_format')
        ui['notice'].configure(text=('Unavailable settings cleared: ' + ', '.join(changed) + '. ' if changed else '') +
            'Settings belong to this stage, not to individual models. Blank optional fields use model defaults. Seed reproducibility is best-effort. Act has no inference.')
        ui['extras'].configure(state='normal' if stage == 'sense' and choices.get(model.get(), {}).get('backend') == 'transformers' else 'disabled')
        format_changed(stage)

    def save_prompt(stage):
        ui = widgets[stage]
        name = simpledialog.askstring('Save prompt', 'New prompt name (filename stem):', parent=root)
        if name is None:
            return
        if not name.strip() or Path(name).name != name or '/' in name or '\\' in name or name in ('.', '..'):
            messagebox.showerror('Invalid name', 'Use a filename stem without path separators.')
            return
        path = ROOT / 'smpa_ros' / 'prompts' / stage / f'{name}.txt'
        if path.exists() and not messagebox.askyesno('Overwrite prompt?', f'Overwrite {name}.txt?', parent=root):
            return
        try:
            candidate = collect()
            validate(candidate)
            path.write_text(read(ui['prompt']), encoding='utf-8')
            ui['prompt_name'].set(name)
            ui['prompt_menu']['values'] = sorted(p.stem for p in path.parent.glob('*.txt'))
        except Exception as error:
            messagebox.showerror('Cannot save prompt', safe(str(error)))

    for stage in STAGES:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text=stage.title())
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(1, weight=3)
        tab.rowconfigure(3, weight=1)
        tab.rowconfigure(5, weight=1)
        selection = ttk.Frame(tab)
        selection.grid(row=0, column=0, sticky='ew', padx=(0, 16))
        ttk.Label(selection, text='Prompt').pack(side='left')
        name = tk.StringVar(value=config['stage_prompts'][stage]['name'])
        menu = ttk.Combobox(selection, textvariable=name, state='readonly', width=23,
                            values=sorted(p.stem for p in (ROOT / 'smpa_ros' / 'prompts' / stage).glob('*.txt')))
        menu.pack(side='left', padx=8)
        ttk.Button(selection, text='Save as…', command=lambda stage=stage: save_prompt(stage)).pack(side='right')
        prompt = ScrolledText(tab, wrap='word', width=55, height=15, undo=True)
        prompt.grid(row=1, column=0, sticky='nsew', padx=(0, 16), pady=6)
        replace(prompt, config['stage_prompts'][stage]['text'])
        ttk.Label(tab, text='Output schema' + (' — fixed action contract; annotations editable' if stage == 'plan' else ' — enabled for json_schema')).grid(row=2, column=0, sticky='w')
        schema = ScrolledText(tab, wrap='word', width=55, height=7, undo=True)
        schema.grid(row=3, column=0, sticky='nsew', padx=(0, 16), pady=6)
        replace(schema, json.dumps(stage_generation(config, stage).get('output_schema', ACTION_SCHEMA if stage == 'plan' else {'type': 'object'}), indent=2))
        ttk.Label(tab, text='Available tools / actions').grid(row=4, column=0, sticky='w')
        tools = ttk.Frame(tab)
        tools.grid(row=5, column=0, sticky='nsew', padx=(0, 16))
        for column, group in enumerate(dict.fromkeys(entry['group'] for entry in AVAILABLE_ACTIONS.values())):
            panel = ttk.LabelFrame(tools, text=group, padding=6)
            panel.grid(row=0, column=column, sticky='nw', padx=(0, 12))
            for action, entry in AVAILABLE_ACTIONS.items():
                if entry['group'] == group:
                    ttk.Checkbutton(panel, text=entry['label'], variable=action_variables[action],
                                    command=actions_changed).pack(anchor='w')
        side = ttk.Frame(tab)
        side.grid(row=0, column=1, rowspan=6, sticky='nsew')
        ttk.Label(side, text='Generation parameters').pack(anchor='w')
        form = ttk.Frame(side)
        form.pack(fill='x', pady=12)
        variables, fields = {}, {}
        for row, (key, label) in enumerate(zip(GENERATION_KEYS, ('Temperature', 'Max output tokens', 'Reasoning effort', 'Seed (optional)', 'Response format'))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', pady=7)
            variable = tk.StringVar()
            field = ttk.Combobox(form, textvariable=variable, state='readonly', width=21) if key in ('reasoning_effort', 'response_format') else ttk.Entry(form, textvariable=variable, width=23)
            field.grid(row=row, column=1, sticky='ew', padx=(10, 0), pady=7)
            variables[key], fields[key] = variable, field
        form.columnconfigure(1, weight=1)
        notice = ttk.Label(side, wraplength=330, text='Settings belong to this stage, not to individual models. Blank optional fields use model defaults. Seed reproducibility is best-effort. Act has no inference.')
        notice.pack(anchor='w', pady=12)
        ttk.Label(side, text='Action timeout (seconds)' if stage == 'act' else 'Shared local model device / dtype' if stage == 'sense' else '').pack(anchor='w')
        extras = ScrolledText(side, wrap='word', width=35, height=4)
        if stage in ('sense', 'act'):
            extras.pack(fill='x', pady=6)
        replace(extras, yaml.safe_dump({'action_timeout': config.get('execution', {}).get('action_timeout', 30.0)} if stage == 'act' else
            {key: config['model'].get(key, 'auto') for key in ('device', 'dtype')} if stage == 'sense' else {}))
        if stage in ('model', 'plan'):
            extras.configure(state='disabled')
        widgets[stage] = dict(prompt=prompt, prompt_name=name, prompt_menu=menu, schema=schema,
                              variables=variables, fields=fields, notice=notice, extras=extras)
        populate(stage)
        fields['response_format'].bind('<<ComboboxSelected>>', lambda event, stage=stage: format_changed(stage))
        menu.bind('<<ComboboxSelected>>', lambda event, stage=stage: replace(widgets[stage]['prompt'],
            (ROOT / 'smpa_ros' / 'prompts' / stage / f"{widgets[stage]['prompt_name'].get()}.txt").read_text()))

    dropdown.bind('<<ComboboxSelected>>', lambda event: [model_changed(stage) for stage in STAGES])

    def collect():
        candidate = deepcopy(config)
        candidate['enabled_actions'] = [name for name, variable in action_variables.items() if variable.get()]
        candidate['scene_refinement'] = dict(enabled=experiment.get(), properties=[item.strip() for item in properties.get().split(',')])
        value = cycles.get()
        if not value.isascii() or not value.isdecimal() or int(value) < 1:
            raise ValueError('Cycles must be a positive integer (minimum 1)')
        candidate.setdefault('execution', {})['max_cycles'] = int(value)
        choice = choices.get(model.get())
        if choice is None:
            raise ValueError('Select a shared vision and text model')
        if choice.get('status') not in (None, 'complete'):
            raise ValueError(choice.get('reason', 'Local model is unavailable'))
        candidate['model'] = {key: value for key, value in choice.items() if key not in ('status', 'reason')}
        for stage, ui in widgets.items():
            candidate['stage_prompts'][stage] = dict(name=ui['prompt_name'].get(), text=read(ui['prompt']))
            values = {}
            for key, variable in ui['variables'].items():
                text = variable.get().strip()
                values[key] = None if not text else float(text) if key == 'temperature' else int(text) if key in ('max_output_tokens', 'seed') else text
            if values['response_format'] == 'json_schema':
                values['output_schema'] = json.loads(read(ui['schema']))
            elif 'output_schema' in candidate['stage_settings'].get(stage, {}):
                # Keep the last valid schema available when switching formats.
                values['output_schema'] = candidate['stage_settings'][stage]['output_schema']
            candidate['stage_settings'][stage] = values
            extras = yaml.safe_load(read(ui['extras']))
            if stage == 'act':
                if not isinstance(extras, dict) or set(extras) != {'action_timeout'}:
                    raise ValueError('Act requires action_timeout only')
                candidate.setdefault('execution', {}).update(extras)
                continue
            if stage == 'sense':
                candidate['prompt'] = ui['prompt_name'].get()
                if not isinstance(extras, dict) or set(extras) != {'device', 'dtype'}:
                    raise ValueError('Sense requires device and dtype only')
                if extras['device'] not in ('auto', 'cpu', 'cuda') or extras['dtype'] not in ('auto', 'float32', 'float16', 'bfloat16'):
                    raise ValueError('Unsupported local device/dtype')
                if choice['backend'] == 'transformers':
                    candidate['model'].update(extras)
        validate(candidate)
        return candidate

    def safe(text):
        for secret in secret_values:
            text = text.replace(secret, '[REDACTED]')
        return text

    def save():
        nonlocal config, choices
        candidate = collect()
        save_configuration(candidate, ROOT / 'config.yaml')
        config = candidate

    def save_clicked():
        try:
            save()
            status.set('Configuration saved.')
        except Exception as error:
            messagebox.showerror('Invalid configuration', safe(str(error)))

    def reload_clicked():
        nonlocal config, choices
        try:
            config = load_config()
            cycles.set(str(config.get('execution', {}).get('max_cycles', 1)))
            for name, variable in action_variables.items():
                variable.set(name in enabled_actions(config))
            experiment.set(refinement_settings(config)['enabled'])
            properties.set(', '.join(refinement_settings(config)['properties']))
            choices = model_choices(config, 'sense')
            dropdown['values'] = list(choices)
            model.set(selected_model_label(config, 'sense', choices))
            for stage, ui in widgets.items():
                ui['prompt_name'].set(config['stage_prompts'][stage]['name'])
                replace(ui['prompt'], config['stage_prompts'][stage]['text'])
                ui['schema'].configure(state='normal')
                replace(ui['schema'], json.dumps(stage_generation(config, stage).get('output_schema', ACTION_SCHEMA if stage == 'plan' else {'type': 'object'}), indent=2))
                ui['extras'].configure(state='normal')
                replace(ui['extras'], yaml.safe_dump({'action_timeout': config.get('execution', {}).get('action_timeout', 30.0)} if stage == 'act' else
                    {key: config['model'].get(key, 'auto') for key in ('device', 'dtype')} if stage == 'sense' else {}))
                populate(stage)
            status.set('Configuration reloaded.')
        except Exception as error:
            messagebox.showerror('Cannot reload configuration', safe(str(error)))

    def launch():
        nonlocal process
        if process is not None:
            return
        try:
            save()
            start_button.configure(state='disabled')
            save_button.configure(state='disabled')
            process = subprocess.Popen([str(ROOT / 'run_smpa.sh'), f'config:={ROOT / "config.yaml"}',
                f"max_cycles:={config.get('execution', {}).get('max_cycles', 1)}"], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            status.set('SMPA running; configuration saving is locked until exit.')

            def monitor(child):
                for line in child.stdout:
                    events.put(('log', safe(line)))
                events.put(('exit', child.wait()))
            threading.Thread(target=monitor, args=(process,), daemon=True).start()
        except Exception as error:
            start_button.configure(state='normal')
            save_button.configure(state='normal')
            messagebox.showerror('Cannot start SMPA', safe(str(error)))

    def poll():
        nonlocal process
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == 'log':
                    log.insert('end', value)
                    log.see('end')
                else:
                    process = None
                    start_button.configure(state='normal')
                    save_button.configure(state='normal')
                    status.set(f'SMPA exited with status {value}. See output above.')
                    if value:
                        messagebox.showerror('SMPA failed', f'Launcher exited with status {value}. See output above.')
        except queue.Empty:
            pass
        root.after(100, poll)

    def close():
        if process is not None:
            if messagebox.askyesno('SMPA running', 'Stop the run and close after cleanup?', parent=root):
                os.killpg(process.pid, signal.SIGINT)
                status.set('Stopping SMPA; waiting for lifecycle cleanup…')
                def await_exit():
                    if process is None:
                        root.destroy()
                    else:
                        root.after(200, await_exit)
                await_exit()
        else:
            root.destroy()

    log = ScrolledText(root, height=6, wrap='word')
    log.pack(fill='x', padx=12)
    footer = ttk.Frame(root, padding=12)
    footer.pack(fill='x')
    status = tk.StringVar(value='Goal and experiment count remain owned by smpa.py. Cycles are saved in config.yaml.')
    ttk.Label(footer, textvariable=status).pack(side='left')
    start_button = ttk.Button(footer, text='Start', command=launch)
    start_button.pack(side='right', padx=(8, 0))
    save_button = ttk.Button(footer, text='Save config', command=save_clicked)
    save_button.pack(side='right', padx=8)
    ttk.Button(footer, text='Reload', command=reload_clicked).pack(side='right')
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(100, poll)
    root.mainloop()


if __name__ == '__main__':
    main()
