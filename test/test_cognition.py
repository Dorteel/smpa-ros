import json
import sys
from unittest.mock import MagicMock

import pytest

from smpa_ros.backends.ollama import OllamaBackend
from smpa_ros.backends.transformers import TransformersBackend
from runner import Runner


def test_cycle_order_and_independent_backends():
    from smpa_ros.smpa import run
    runner = MagicMock()
    run(runner, 2)
    assert [call[0] for call in runner.method_calls] == [
        'start', 'cycle', 'sense', 'model', 'plan', 'act',
        'cycle', 'sense', 'model', 'plan', 'act', 'finish']
    runner.model.assert_called_with(runner.sense.return_value, prompt='default')
    runner.plan.assert_called_with(runner.model.return_value, prompt='default')
    runner.act.assert_called_with(runner.plan.return_value, prompt='default')


def test_failed_stage_stops_cycle():
    from smpa_ros.smpa import run
    runner = MagicMock()
    runner.sense.side_effect = RuntimeError('inference failed')
    with pytest.raises(RuntimeError, match='inference failed'):
        run(runner, 1)
    runner.model.assert_not_called()
    runner.plan.assert_not_called()
    runner.act.assert_not_called()


def test_ollama_request_without_server(monkeypatch):
    requests = []

    def respond(request, timeout):
        requests.append((request, timeout))
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"response": "facts"}'
        return response

    monkeypatch.setattr('smpa_ros.backends.ollama.urlopen', respond)
    backend = OllamaBackend('local-model', timeout=5)
    assert backend.generate('observation', max_new_tokens=12) == 'facts'
    request, timeout = requests[0]
    assert request.full_url == 'http://localhost:11434/api/generate'
    assert timeout == 5
    assert json.loads(request.data) == {
        'model': 'local-model', 'prompt': 'observation', 'stream': False,
        'options': {'num_predict': 12},
    }


@pytest.mark.parametrize('chat_template', [None, 'test template'])
def test_transformers_local_loading_and_continuation(monkeypatch, chat_template):
    torch = MagicMock()
    transformers = MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    tokenizer = transformers.AutoTokenizer.from_pretrained.return_value
    tokenizer.chat_template = chat_template
    tokenizer.apply_chat_template.return_value = 'formatted prompt'
    input_ids = MagicMock()
    input_ids.shape = (1, 3)
    tokenizer.return_value.to.return_value = {'input_ids': input_ids}
    tokenizer.decode.return_value = 'continuation'
    model = transformers.AutoModelForCausalLM.from_pretrained.return_value

    backend = TransformersBackend('/local/checkpoint')
    assert backend.generate('prompt', max_new_tokens=7) == 'continuation'
    transformers.AutoTokenizer.from_pretrained.assert_called_once_with(
        '/local/checkpoint', local_files_only=True,
    )
    transformers.AutoModelForCausalLM.from_pretrained.assert_called_once_with(
        '/local/checkpoint', local_files_only=True,
    )
    model.eval.assert_called_once_with()
    model.generate.assert_called_once_with(
        input_ids=input_ids, max_new_tokens=7,
    )
    model.generate.return_value.__getitem__.assert_called_once_with((0, slice(3, None)))
    torch.no_grad.assert_called_once_with()
    if chat_template:
        tokenizer.assert_called_once_with(
            'formatted prompt', return_tensors='pt', add_special_tokens=False,
        )
    else:
        tokenizer.assert_called_once_with('prompt', return_tensors='pt')


@pytest.mark.integration
def test_local_llm_world_model_iterations(tmp_path):
    """Optional genuine CPU inference using the already provisioned modeling checkpoint."""
    import importlib.util
    from pathlib import Path
    import yaml

    repository = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repository / 'config.yaml').read_text())
    if config.get('model', {}).get('backend') == 'api':
        pytest.skip('API modeling is configured; no local modeling checkpoint selected')
    registry = yaml.safe_load((repository / 'models/model_library.yaml').read_text())['models']
    selected = registry['qwen3-0.6b']
    checkpoint = repository / 'models' / selected.get('path', '')
    if (selected['backend'] != 'transformers' or not (checkpoint / 'model.safetensors').is_file()
            or importlib.util.find_spec('torch') is None or importlib.util.find_spec('transformers') is None):
        pytest.skip('Optional local Transformers modeling checkpoint/dependencies are unavailable; no download attempted')
    import torch
    previous_threads = torch.get_num_threads()
    torch.set_num_threads(2)
    try:
        backend = TransformersBackend(str(checkpoint), enable_thinking=selected.get('enable_thinking'))
        assert isinstance(backend.model, torch.nn.Module)
        observations = ['A white cup is on a wooden table.',
                        'The same white cup is still on the table. A red book is now next to it.']
        from test_run_storage import run
        directory = run(2, root=tmp_path, sense=lambda directory, cycle: observations[cycle - 1],
                        model=backend, model_identifier=selected['model_id'],
                        plan=lambda prompt, **settings: '{"action":"look_left"}',
                        act=lambda action, record: record.update(goal_accepted=True, result_status=4, success=True))
        snapshots = [(directory / f'world_model/iteration_{cycle:03d}.md').read_text() for cycle in (1, 2)]
        for snapshot in snapshots:
            assert '# World Model' in snapshot and '## Entities' in snapshot
            assert 'cup' in snapshot.lower()
            assert snapshot not in observations
        second = json.loads((directory / 'state/cycle_000002/model.json').read_text())
        assert snapshots[0] in second['prompt']
        assert second['output'] == snapshots[1]
        assert second['model_identifier'] == selected['model_id']
        assert json.loads((directory / 'state/cycle_000002/plan.json').read_text())['input'] == snapshots[1]
        assert yaml.safe_load((directory / 'metadata.yaml').read_text())['completed_cycles'] == 2
        print(f'Genuine local LLM inference: {selected["model_id"]}, two persisted snapshots')
    finally:
        torch.set_num_threads(previous_threads)


@pytest.mark.parametrize('available', [True, False])
def test_transformers_schema_constraints_never_fall_back(monkeypatch, available):
    from types import ModuleType
    from smpa_ros.plan import ACTION_SCHEMA
    torch = MagicMock()
    transformers = MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    library = ModuleType('outlines')
    library.from_transformers = MagicMock()
    processors = ModuleType('outlines.backends')
    processors.get_json_schema_logits_processor = MagicMock(return_value='token-mask')
    monkeypatch.setitem(sys.modules, 'outlines', library if available else None)
    monkeypatch.setitem(sys.modules, 'outlines.backends', processors if available else None)
    tokenizer = transformers.AutoTokenizer.from_pretrained.return_value
    tokenizer.chat_template = 'test'
    tokenizer.decode.return_value = '{"action":"look_left"}'
    tokenizer.return_value.to.return_value = {'input_ids': MagicMock(shape=(1, 3))}
    backend = TransformersBackend('/local/model', enable_thinking=False)
    if available:
        assert backend.generate('choose', schema=ACTION_SCHEMA) == '{"action":"look_left"}'
        library.from_transformers.assert_called_once_with(backend.model, backend.tokenizer)
        processors.get_json_schema_logits_processor.assert_called_once_with(
            'outlines_core', library.from_transformers.return_value, json.dumps(ACTION_SCHEMA))
        assert backend.model.generate.call_args.kwargs['logits_processor'] == ['token-mask']
    else:
        with pytest.raises(RuntimeError, match='[Nn]o unrestricted fallback'):
            backend.generate('choose', schema=ACTION_SCHEMA)
        backend.model.generate.assert_not_called()


@pytest.mark.parametrize('backend_name', ['transformers', 'openai', 'nebula', 'ollama'])
def test_shared_text_backend_selection(tmp_path, monkeypatch, backend_name):
    from pathlib import Path
    import yaml
    from smpa_ros.model import load_backend
    from smpa_ros.plan import ACTION_SCHEMA

    repository = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repository / 'config.yaml').read_text())
    vision = dict(config['model'])
    registry = yaml.safe_load((repository / 'models/model_library.yaml').read_text())
    registry['models']['test-ollama'] = {'backend': 'ollama', 'model_id': 'test-model'}
    (tmp_path / 'model_library.yaml').write_text(yaml.safe_dump(registry))
    if backend_name in ('openai', 'nebula'):
        config['modeling'] = {'backend': 'api', 'provider': backend_name}
        target = 'smpa_ros.backends.api.APIBackend'
    elif backend_name == 'transformers':
        config['modeling'] = {'model': 'qwen3-0.6b'}
        target = 'smpa_ros.backends.transformers.TransformersBackend'
    else:
        config['modeling'] = {'model': 'test-ollama'}
        target = 'smpa_ros.backends.ollama.OllamaBackend'
    constructor = MagicMock()
    constructor.return_value.generate.return_value = 'generated text'
    monkeypatch.setattr(target, constructor)
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    if backend_name == 'transformers':
        local_model_directory(tmp_path / 'weights/llm/qwen3_0.6b')
    generate, metadata = load_backend(config, config_path, tmp_path / 'weights', tmp_path)
    constructor.assert_not_called()
    assert config['model'] == vision
    assert generate('world', max_new_tokens=37) == 'generated text'
    if backend_name in ('openai', 'nebula'):
        constructor.assert_called_once_with(config_path, modality='text', provider=backend_name)
        assert metadata['model_id'] == config[backend_name]['models']['text']
    elif backend_name == 'transformers':
        constructor.assert_called_once_with(str(tmp_path / 'weights/llm/qwen3_0.6b'), enable_thinking=False)
        assert metadata['model_id'] == 'Qwen/Qwen3-0.6B'
    else:
        constructor.assert_called_once_with('test-model')
        assert metadata['model_id'] == 'test-model'
    options = {'reasoning_effort': 'low'} if backend_name == 'nebula' else {}
    generate('choose', max_new_tokens=11, schema=ACTION_SCHEMA, **options)
    constructor.return_value.generate.assert_called_with(
        'choose', max_new_tokens=11, schema=ACTION_SCHEMA, **options)
    assert constructor.call_count == 1


@pytest.mark.parametrize('stage, expected', [('sense', 4096), ('model', 4096), ('plan', 4096), ('act', None)])
def test_unified_stage_generation_defaults(stage, expected):
    import importlib
    from pathlib import Path
    import yaml
    module = importlib.import_module('smpa_ros.' + stage)
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config.yaml').read_text())
    from smpa_ros.backends.base import stage_generation
    assert module.load_prompt('input')[1] == stage_generation(config, stage)
    assert module.load_prompt('input', configuration=config)[1]['max_output_tokens'] == expected
    if stage != 'act':
        config['stage_settings'][stage]['max_output_tokens'] = 37
        assert module.load_prompt('input', configuration=config)[1]['max_output_tokens'] == 37



@pytest.mark.parametrize('stage', ['sense', 'model', 'plan', 'act'])
def test_canonical_generation_and_legacy_overrides(stage):
    from copy import deepcopy
    from smpa_ros.backends.base import GENERATION_KEYS, stage_generation
    config = {'stage_settings': {name: dict.fromkeys(GENERATION_KEYS) if name == 'act' else
              dict(temperature=.2, max_output_tokens=17, reasoning_effort=None, seed=7,
                   response_format='json_schema' if name == 'plan' else 'text')
              for name in ('sense', 'model', 'plan', 'act')}}
    previous = deepcopy(config)
    settings = stage_generation(config, stage)
    assert config == previous
    assert set(GENERATION_KEYS) <= set(settings)
    if stage == 'act':
        assert all(value is None for value in settings.values())
        config['stage_settings']['act']['temperature'] = .5
        with pytest.raises(ValueError, match='no inference'):
            stage_generation(config, stage)
    else:
        config['stage_settings'][stage]['seed'] = 42
        assert stage_generation(config, stage)['seed'] == 42
        for other in set(previous['stage_settings']) - {stage}:
            assert config['stage_settings'][other] == previous['stage_settings'][other]
        section = {'sense': 'vision', 'model': 'modeling', 'plan': 'planning'}[stage]
        config[section] = {'max_output_tokens': 99, 'temperature': .8}
        assert stage_generation(config, stage)['max_output_tokens'] == 99
        assert stage_generation(config, stage)['temperature'] == .8
        config['stage_settings'][stage] = {'max_new_tokens': 12}
        config.pop(section)
        assert stage_generation(config, stage)['max_output_tokens'] == 12


@pytest.mark.parametrize('bad', [dict(temperature=float('nan')), dict(temperature=True),
    dict(temperature=-.1), dict(max_output_tokens=0), dict(max_output_tokens=None),
    dict(seed=1.2), dict(seed=True), dict(reasoning_effort='unknown'),
    dict(response_format='xml'), dict(response_format='json_schema', output_schema={'type':'invalid'}),
    dict(response_format='json_schema', output_schema={'$ref':'https://example.org/schema'})])
def test_generation_rejects_invalid_values(bad):
    from smpa_ros.backends.base import validate_generation
    with pytest.raises(ValueError):
        validate_generation(bad)


def test_unsupported_generation_controls():
    from smpa_ros.backends.base import generation_capabilities, validate_generation
    for backend in ('transformers', 'ollama'):
        with pytest.raises(ValueError, match='reasoning_effort'):
            validate_generation({'reasoning_effort': 'low'}, backend=backend)
    validate_generation(dict(response_format='json_object'),
                        capabilities=generation_capabilities('transformers', vision=True))
    config = {'models': {'text': 'configured-model'}, 'generation_capabilities': {'text': {
        'parameters':['max_output_tokens'], 'response_formats':['text'], 'reasoning_efforts':[]}}}
    for identifier in ('configured-model', 'unknown-model'):
        with pytest.raises(ValueError, match='seed'):
            validate_generation({'seed': 10}, capabilities=generation_capabilities(
                'api', provider=config, model_id=identifier))


@pytest.mark.parametrize('mode', ['json_object', 'json_schema'])
def test_ollama_structured_settings_and_validation(monkeypatch, mode):
    calls = []
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"response":"{\\"answer\\":3}"}'
    def request(message, timeout):
        calls.append(json.loads(message.data))
        return response
    monkeypatch.setattr('smpa_ros.backends.ollama.urlopen', request)
    schema = {'type':'object','properties':{'answer':{'type':'integer'}}, 'required':['answer']}
    kwargs = {'schema':schema} if mode == 'json_schema' else {}
    backend = OllamaBackend('configured')
    assert json.loads(backend.generate('Return JSON', max_new_tokens=31,
        temperature=.6, seed=19, response_format=mode, **kwargs)) == {'answer':3}
    assert calls[0]['options'] == {'temperature':.6,'seed':19,'num_predict':31}
    assert calls[0]['format'] == (schema if mode == 'json_schema' else 'json')
    response.__enter__.return_value.read.return_value = b'{"response":"[]"}'
    with pytest.raises(RuntimeError, match='invalid JSON'):
        backend.generate('Return JSON', response_format=mode, **kwargs)
    with pytest.raises(ValueError, match='reasoning_effort'):
        backend.generate('test', reasoning_effort='high')


def test_transformers_sampling_seed_and_json_object(monkeypatch):
    from types import ModuleType
    torch, transformers = MagicMock(), MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    outlines, processors = ModuleType('outlines'), ModuleType('outlines.backends')
    outlines.from_transformers = MagicMock()
    processors.get_json_schema_logits_processor = MagicMock(return_value='mask')
    monkeypatch.setitem(sys.modules, 'outlines', outlines)
    monkeypatch.setitem(sys.modules, 'outlines.backends', processors)
    tokenizer = transformers.AutoTokenizer.from_pretrained.return_value
    tokenizer.return_value.to.return_value = {'input_ids':MagicMock(shape=(1,3))}
    tokenizer.decode.return_value = '{"answer":3}'
    backend = TransformersBackend('/existing')
    assert backend.generate('JSON', max_new_tokens=22, temperature=.7, seed=32,
                            response_format='json_object') == '{"answer":3}'
    options = backend.model.generate.call_args.kwargs
    assert options['do_sample'] is True and options['temperature'] == .7
    assert options['max_new_tokens'] == 22 and options['logits_processor'] == ['mask']
    torch.manual_seed.assert_called_once_with(32)
    torch.random.fork_rng.assert_called_once()
    backend.generate('text', temperature=0)
    assert backend.model.generate.call_args.kwargs['do_sample'] is False
    assert 'temperature' not in backend.model.generate.call_args.kwargs
    with pytest.raises(ValueError, match='reasoning_effort'):
        backend.generate('text', reasoning_effort='low')
    tokenizer.decode.return_value = '[]'
    with pytest.raises(RuntimeError, match='invalid JSON'):
        backend.generate('JSON', response_format='json_object')


def test_gui_canonical_load_edit_save_and_schema_preservation(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import yaml
    repository = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('run_configurator', repository / 'scripts/run_configurator.py')
    gui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gui)
    config = gui.load_config()
    assert set(config['stage_settings']) == set(gui.STAGES)
    assert all(key in config['stage_settings'][stage] for stage in gui.STAGES for key in gui.GENERATION_KEYS)
    for section in ('vision', 'modeling', 'planning'):
        assert not set(gui.GENERATION_KEYS) & set(config.get(section, {}))
    config['stage_settings']['model'].update(temperature=.35, max_output_tokens=87, response_format='json_schema',
        output_schema={'type':'object','properties':{'objects':{'type':'array'}},'required':['objects'],'additionalProperties':False})
    config.setdefault('execution', {})['max_cycles'] = 3
    destination = tmp_path / 'config.yaml'
    gui.save_configuration(config, destination)
    saved = destination.read_bytes()
    assert yaml.safe_load(saved) == config
    assert isinstance(config['model'], dict) and 'modeling' not in config and 'planning' not in config
    import shutil
    shutil.copytree(repository / 'smpa_ros/prompts', tmp_path / 'smpa_ros/prompts')
    (tmp_path / 'models').symlink_to(repository / 'models', target_is_directory=True)
    monkeypatch.setattr(gui, 'ROOT', tmp_path)
    assert gui.load_config() == config
    for invalid in (0, -1, 1.5, True, '2', None):
        config['execution']['max_cycles'] = invalid
        with pytest.raises(ValueError, match='positive integer'):
            gui.save_configuration(config, destination)
        assert destination.read_bytes() == saved
    config['execution']['max_cycles'] = 3
    config['stage_settings']['model']['output_schema'] = {'type':'bad-schema'}
    with pytest.raises(ValueError, match='Schema'):
        gui.save_configuration(config, destination)
    assert destination.read_bytes() == saved
    config = gui.load_config()
    config['stage_settings']['act']['seed'] = 42
    with pytest.raises(ValueError, match='no inference'):
        gui.validate(config)


def test_omitted_canonical_optional_parameters_preserve_model_defaults():
    from smpa_ros.backends.base import generation_arguments, stage_generation
    config = {'modeling':{'backend':'ollama'}, 'stage_settings':{'model':{'max_output_tokens':39}}}
    settings = stage_generation(config, 'model')
    assert settings['temperature'] is None and settings['seed'] is None
    assert generation_arguments(settings) == {'max_new_tokens':39,'response_format':'text'}
    legacy = stage_generation({'modeling':{'backend':'ollama'}, 'stage_settings':{'model':{'max_new_tokens':39}}}, 'model')
    assert legacy['temperature'] == 0 and legacy['seed'] == 0


def local_model_directory(path, vision=False):
    """Small file fixture; inference is mocked, filesystem validation is real."""
    path.mkdir(parents=True, exist_ok=True)
    (path / 'config.json').write_text(json.dumps({'model_type':'qwen3_vl' if vision else 'qwen3'}))
    (path / 'tokenizer.json').write_text('{}')
    (path / 'model.safetensors').write_bytes(b'fixture weights')
    if vision:
        (path / 'preprocessor_config.json').write_text('{}')
    return path


def test_directory_discovery_without_registry_entries(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import yaml
    source = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('gui_discovery', source / 'scripts/run_configurator.py')
    gui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gui)
    config = gui.load_config()
    monkeypatch.setattr(gui, 'ROOT', tmp_path)
    (tmp_path / 'models').mkdir()
    (tmp_path / 'models/model_library.yaml').write_text('models: {}\n')
    names = ['qwen2.5_vl_3b','qwen3_vl_2b','qwen3_vl_4b','smolvlm2_2.2b','smolvlm2_500m']
    for name in names:
        local_model_directory(tmp_path / 'models/vlm' / name, vision=True)
    local_model_directory(tmp_path / 'models/llm/new_text_model')
    partial = local_model_directory(tmp_path / 'models/vlm/partial', vision=True)
    (partial / 'model.safetensors').unlink()
    unsupported = local_model_directory(tmp_path / 'models/vlm/unsupported', vision=True)
    (unsupported / 'config.json').write_text('{"model_type":"unknown"}')
    choices = gui.model_choices(config, 'sense')
    locals_ = {choice['model']:choice for choice in choices.values() if choice['backend']=='transformers'}
    assert all(locals_[name]['status']=='complete' for name in names)
    assert locals_['partial']['status']=='incomplete'
    assert locals_['unsupported']['status']=='unavailable'
    assert any(choice.get('provider')=='openai' for choice in choices.values())
    assert any(choice.get('provider')=='nebula' for choice in choices.values())
    assert all(Path(locals_[name]['path']).is_absolute() for name in names)
    assert any(c.get('model')=='new_text_model' for c in gui.model_choices(config,'model').values())
    config['model'] = {'backend':'transformers','path':locals_[names[0]]['path']}
    assert gui.selected_model_label(config,'sense',choices)
    assert gui.model_choices(config,'act') == {}


def test_local_shards_and_downloader_manifest_validation(tmp_path):
    import shutil
    from pathlib import Path
    from smpa_ros.model import local_model_info
    root = tmp_path / 'models'
    directory = local_model_directory(root / 'vlm/test',vision=True)
    (tmp_path / 'scripts').mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / 'scripts/download_models.py', tmp_path / 'scripts/download_models.py')
    (directory / 'model.safetensors').unlink()
    index = directory / 'model.safetensors.index.json'
    index.write_text(json.dumps({'weight_map':{'layer':'model-00001-of-00001.safetensors'}}))
    assert local_model_info(directory,vision=True,model_root=root)['status']=='incomplete'
    shard = directory / 'model-00001-of-00001.safetensors'
    shard.write_bytes(b'weights')
    assert local_model_info(directory,vision=True,model_root=root)['status']=='complete'
    report = dict(models=[dict(repo_id='test/model',local_dir=str(directory),files={shard.name:{'size':8}})])
    (root / 'download_report.json').write_text(json.dumps(report))
    info = local_model_info(directory,vision=True,model_root=root)
    assert info['status']=='incomplete' and 'manifest' in info['reason']
    report['models'][0]['files'][shard.name]['size']=7
    (root / 'download_report.json').write_text(json.dumps(report))
    info = local_model_info(directory,vision=True,model_root=root)
    assert info['status']=='complete' and info['model_id']=='test/model'


def test_explicit_text_path_and_missing_selection_never_use_hub(tmp_path,monkeypatch):
    from smpa_ros.model import load_backend
    model = local_model_directory(tmp_path / 'llm/new_model')
    constructor=MagicMock()
    monkeypatch.setattr('smpa_ros.backends.transformers.TransformersBackend',constructor)
    config={'modeling':{'backend':'transformers','path':str(model),'model_id':'custom/new-model'}}
    generate,selected=load_backend(config,tmp_path/'config.yaml',tmp_path,tmp_path/'no_registry')
    assert selected['path']==str(model.resolve()) and selected['model_id']=='custom/new-model'
    generate('input',max_new_tokens=7)
    constructor.assert_called_once_with(str(model),enable_thinking=None)
    (model/'model.safetensors').unlink()
    with pytest.raises(ValueError,match='incomplete'):
        load_backend(config,tmp_path/'config.yaml',tmp_path,tmp_path/'no_registry')
    assert constructor.call_count==1
