import sys
from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import DescribeScene

from smpa_ros.sense import SenseService, image_to_pil
from smpa_ros.backends.transformers import TransformersVLM


def test_image_conversion_with_padding():
    image = Image(width=1, height=2, encoding='bgr8', step=4,
                  data=[0, 0, 255, 0, 0, 255, 0, 0])
    decoded = image_to_pil(image)
    assert list(decoded.getdata()) == [(255, 0, 0), (0, 255, 0)]


@pytest.mark.parametrize('image', [
    Image(), Image(width=1, height=1, encoding='rgb8', step=3, data=[0]),
    Image(width=1, height=1, encoding='rgb8', step=2, data=[0, 0]),
])
def test_invalid_image(image):
    with pytest.raises(ValueError):
        image_to_pil(image)


def test_vlm_processor_and_local_loading(monkeypatch):
    torch = MagicMock()
    transformers = MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    processor = transformers.AutoProcessor.from_pretrained.return_value
    processor.apply_chat_template.return_value = 'image prompt'
    processor.decode.return_value = ' A scene. '
    input_ids = MagicMock()
    input_ids.shape = (1, 4)
    processor.return_value.to.return_value = {'input_ids': input_ids}
    backend = TransformersVLM('cached/model')
    image = object()
    assert backend.describe(image, 'Describe.', max_new_tokens=7) == 'A scene.'
    transformers.AutoProcessor.from_pretrained.assert_called_once_with(
        'cached/model', local_files_only=True,
    )
    transformers.AutoModelForImageTextToText.from_pretrained.assert_called_once_with(
        'cached/model', local_files_only=True, dtype=torch.float16,
    )
    transformers.AutoModelForImageTextToText.from_pretrained.return_value.to.assert_called_once_with('cuda:0')
    processor.assert_called_once_with(
        text='image prompt', images=[image], return_tensors='pt', add_special_tokens=False,
    )
    backend.model.generate.assert_called_once_with(
        input_ids=input_ids, max_new_tokens=7,
    )
    backend.model.generate.return_value.__getitem__.assert_called_once_with((0, slice(4, None)))
    torch.no_grad.assert_called_once_with()


@pytest.mark.parametrize('vision, expected_limit', [
    ({}, 128),
    ({'backend': 'api'}, 512),
    ({'backend': 'api', 'max_output_tokens': 73}, 73),
])
def test_sense_service_round_trip(tmp_path, vision, expected_limit):
    """Use real ROS service serialization/discovery with a mock VLM only."""
    import yaml
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'model': 'smolvlm2', 'vision': vision}))
    rclpy.init(args=['--ros-args', '-p', f'config:={config}'])
    executor = SingleThreadedExecutor()
    server = client_node = None
    try:
        backend = MagicMock()
        backend.describe.return_value = 'A red pixel.'
        server = SenseService(backend=backend)
        client_node = rclpy.create_node('sense_test_client')
        executor.add_node(server)
        executor.add_node(client_node)
        client = client_node.create_client(DescribeScene, '/sense')
        assert client.wait_for_service(timeout_sec=5), 'Sense service was not discovered'
        image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
        for prompt, expected in [('', 'Describe the scene.'), ('What color?', 'What color?')]:
            future = client.call_async(DescribeScene.Request(image=image, prompt=prompt))
            executor.spin_until_future_complete(future, timeout_sec=10)
            assert future.done(), 'Sense request timed out'
            response = future.result()
            assert response.error == ''
            assert response.description == 'A red pixel.'
            args, kwargs = backend.describe.call_args
            assert args[0].getpixel((0, 0)) == (255, 0, 0)
            assert args[1] == expected
            assert kwargs == {'max_new_tokens': expected_limit, 'temperature': 0, 'response_format': 'text'}
        future = client.call_async(DescribeScene.Request())
        executor.spin_until_future_complete(future, timeout_sec=10)
        assert future.done()
        assert future.result().error
        assert future.result().description == ''
        assert backend.describe.call_count == 2
    finally:
        executor.shutdown()
        if client_node is not None:
            client_node.destroy_node()
        if server is not None:
            server.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '512', None])
def test_invalid_vision_token_budget(tmp_path, value):
    import yaml
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'model': 'smolvlm2',
                                     'vision': {'max_output_tokens': value}}))
    rclpy.init(args=['--ros-args', '-p', f'config:={config}'])
    server = SenseService.__new__(SenseService)
    try:
        with pytest.raises(ValueError, match='positive integer'):
            server.__init__(backend=MagicMock())
    finally:
        server.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('budget, modality', [(512, 'vision'), (97, 'model'), (43, 'plan')])
def test_api_request_token_budget(budget, modality):
    from types import SimpleNamespace
    from PIL import Image as PILImage
    from smpa_ros.backends.api import APIBackend
    backend = APIBackend.__new__(APIBackend)
    backend.model_id = 'mock-model'
    backend._client = MagicMock(api_key='dummy-secret')
    backend._client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
            content='{"action":"look_left"}', refusal=None))], usage=None)
    if modality == 'vision':
        backend.describe(PILImage.new('RGB', (1, 1)), 'describe', max_new_tokens=budget)
    else:
        schema = {'type': 'object'} if modality == 'plan' else None
        backend.generate('prompt', max_new_tokens=budget, schema=schema)
    options = backend._client.chat.completions.create.call_args.kwargs
    assert options['max_completion_tokens'] == budget
    if modality == 'plan':
        assert options['response_format']['json_schema']['strict'] is True


@pytest.mark.parametrize('effort', [None, 'low', 'high'])
def test_api_reasoning_control_and_safe_metadata(effort):
    import logging
    from types import SimpleNamespace as N
    from smpa_ros.backends.api import APIBackend
    backend = APIBackend.__new__(APIBackend)
    backend.model_id = 'mock-model'
    backend._client = MagicMock(api_key='test-secret-key')
    response = N(choices=[N(finish_reason='stop', message=N(
        content='visible answer', reasoning_content='private reasoning text', refusal=None))],
        usage=N(prompt_tokens=5, completion_tokens=20, total_tokens=25,
                completion_tokens_details=N(reasoning_tokens=12)))
    backend._client.chat.completions.create.return_value = response
    from unittest.mock import patch
    with patch.object(logging.getLogger('smpa_ros.backends.api'), 'info') as log:
        assert backend.generate('prompt', max_new_tokens=512, reasoning_effort=effort) == 'visible answer'
    options = backend._client.chat.completions.create.call_args.kwargs
    assert options['max_completion_tokens'] == 512
    assert ('reasoning_effort' in options) == (effort is not None)
    if effort is not None:
        assert options['reasoning_effort'] == effort
    logged = log.call_args.args[0] % log.call_args.args[1:]
    assert 'reasoning_tokens=12' in logged
    assert 'reasoning_content_present=True' in logged
    assert 'private reasoning text' not in logged
    assert 'test-secret-key' not in logged
    response.choices[0].finish_reason = 'length'
    response.choices[0].message.content = ''
    with pytest.raises(RuntimeError, match='truncated'):
        backend.generate('prompt', reasoning_effort=effort)


@pytest.mark.parametrize('provider', ['openai', 'nebula'])
@pytest.mark.parametrize('modality', ['text', 'vision'])
def test_unified_provider_config(tmp_path, monkeypatch, provider, modality):
    from pathlib import Path
    from unittest.mock import patch
    import yaml
    from smpa_ros.backends.api import APIBackend

    source = Path(__file__).resolve().parents[1] / 'config.yaml'
    config = tmp_path / 'config.yaml'
    config.write_text(source.read_text())
    settings = yaml.safe_load(config.read_text())[provider]
    key_name = settings['api_key_env']
    monkeypatch.setenv(key_name, 'temporary-test-value')
    monkeypatch.delenv(key_name)
    (tmp_path / '.env').write_text(f'{key_name}=test-config-secret\n')
    monkeypatch.chdir(tmp_path.parent)
    with patch('openai.OpenAI') as client:
        backend = APIBackend(config, provider=provider, modality=modality)
        assert backend.model_id == settings['models'][modality]
        assert client.call_args.kwargs['base_url'] == settings['base_url'].removesuffix('/chat/completions')
        assert client.call_args.kwargs['timeout'] == settings['timeout']
        assert client.call_args.kwargs['api_key'] == 'test-config-secret'
        monkeypatch.setenv(key_name, 'test-environment-secret')
        APIBackend(config, provider=provider, modality=modality)
        assert client.call_args.kwargs['api_key'] == 'test-environment-secret'


@pytest.mark.parametrize('modality', ['text', 'vision'])
@pytest.mark.parametrize('mode', ['text', 'json_object', 'json_schema'])
def test_api_unified_payload_and_output_validation(modality, mode):
    from types import SimpleNamespace as N
    from PIL import Image as PILImage
    from smpa_ros.backends.api import APIBackend
    backend = APIBackend.__new__(APIBackend)
    backend.model_id = 'test-reasoning-model'
    backend._client = MagicMock(api_key='test-api-secret')
    message = N(content='{"answer": 3}' if mode != 'text' else 'answer', refusal=None)
    backend._client.chat.completions.create.return_value = N(
        choices=[N(finish_reason='stop', message=message)], usage=None)
    schema = {'type':'object','properties':{'answer':{'type':'integer'}},'required':['answer'], 'additionalProperties':False}
    kwargs = dict(max_new_tokens=127, temperature=.3, reasoning_effort='low', seed=17,
                  response_format=mode, **({'schema': schema} if mode == 'json_schema' else {}))
    def generate():
        if modality == 'text':
            return backend.generate('Return JSON', **kwargs)
        return backend.describe(PILImage.new('RGB', (1,1)), 'Return JSON', **kwargs)
    assert generate() == message.content
    options = backend._client.chat.completions.create.call_args.kwargs
    assert options['max_completion_tokens'] == 127
    assert options['temperature'] == .3 and options['seed'] == 17 and options['reasoning_effort'] == 'low'
    if mode == 'text':
        assert 'response_format' not in options
    elif mode == 'json_object':
        assert options['response_format'] == {'type':'json_object'}
    else:
        assert options['response_format']['json_schema'] == dict(name='smpa_response', strict=True, schema=schema)
    if mode != 'text':
        message.content = '[]'
        with pytest.raises(RuntimeError, match='invalid JSON'):
            generate()
        message.content = '{"answer":"wrong"}'
        if mode == 'json_schema':
            with pytest.raises(RuntimeError, match='violating'):
                generate()
    before = backend._client.chat.completions.create.call_count
    kwargs['temperature'] = -1
    with pytest.raises(ValueError, match='temperature'):
        generate()
    assert backend._client.chat.completions.create.call_count == before


def test_api_omits_optional_controls_and_rejects_declared_unsupported():
    from types import SimpleNamespace as N
    from smpa_ros.backends.api import APIBackend
    backend = APIBackend.__new__(APIBackend)
    backend.model_id = 'configured'
    backend.modality = 'text'
    backend.provider_settings = {'models':{'text':'configured'}, 'generation_capabilities':{'text':{
        'parameters':['max_output_tokens'], 'response_formats':['text'], 'reasoning_efforts':[]}}}
    backend._client = MagicMock(api_key='test-secret')
    backend._client.chat.completions.create.return_value = N(
        choices=[N(finish_reason='stop',message=N(content='text', refusal=None))],usage=None)
    backend.generate('test')
    options = backend._client.chat.completions.create.call_args.kwargs
    assert not {'temperature', 'seed', 'reasoning_effort', 'response_format'} & set(options)
    for kwargs in ({'seed':2}, {'reasoning_effort':'low'}, {'response_format':'json_object'}):
        with pytest.raises(ValueError, match='unsupported'):
            backend.generate('Return JSON', **kwargs)
    assert backend._client.chat.completions.create.call_count == 1


def test_sense_independent_generation_forwarding(tmp_path):
    import yaml
    config = {'model':'smolvlm2','vision':{'backend':'api'}, 'stage_settings':{'sense':{
        'temperature':.4, 'max_output_tokens':41, 'reasoning_effort':'medium',
        'seed':123, 'response_format':'json_schema', 'output_schema':{'type':'object'}}}}
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    rclpy.init(args=['--ros-args','-p',f'config:={path}'])
    server = None
    try:
        backend = MagicMock()
        backend.describe.return_value = '{}'
        server = SenseService(backend=backend)
        request = DescribeScene.Request(image=Image(width=1,height=1,encoding='rgb8',step=3,data=[255,0,0]), prompt='Exact prompt')
        result = server.describe(request, DescribeScene.Response())
        assert not result.error and result.description == '{}'
        assert backend.describe.call_args.args[1] == 'Exact prompt'
        assert backend.describe.call_args.kwargs == dict(temperature=.4,max_new_tokens=41,
            reasoning_effort='medium',seed=123,response_format='json_schema',schema={'type':'object'})
    finally:
        if server is not None:
            server.destroy_node()
        rclpy.shutdown()


def test_vlm_sampling_seed_and_unsupported_controls(monkeypatch):
    torch, transformers = MagicMock(), MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    processor = transformers.AutoProcessor.from_pretrained.return_value
    processor.return_value.to.return_value = {'input_ids': MagicMock(shape=(1,4))}
    processor.decode.return_value = 'scene'
    backend = TransformersVLM('existing-local-model', device='cpu', dtype='float32')
    assert backend.describe(object(), 'Exact prompt', temperature=.5, seed=13, max_new_tokens=31) == 'scene'
    options = backend.model.generate.call_args.kwargs
    assert options['temperature'] == .5 and options['do_sample'] is True and options['max_new_tokens'] == 31
    torch.manual_seed.assert_called_once_with(13)
    torch.random.fork_rng.assert_called_once_with()
    calls = backend.model.generate.call_count
    for kwargs in ({'reasoning_effort':'low'}, {'schema':{'type':'object'},'response_format':'text'}):
        with pytest.raises(ValueError):
            backend.describe(object(), 'Exact prompt', **kwargs)
    assert backend.model.generate.call_count == calls


def test_sense_explicit_local_path_and_missing_weights(tmp_path,monkeypatch):
    import yaml
    from test_cognition import local_model_directory
    directory=local_model_directory(tmp_path/'models/vlm/unregistered',vision=True)
    path=tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump({'vision':{'backend':'transformers','path':str(directory),
                                            'model_id':'custom/vision'}}))
    constructor=MagicMock()
    monkeypatch.setattr('smpa_ros.sense.TransformersVLM',constructor)
    rclpy.init(args=['--ros-args','-p',f'config:={path}'])
    server=None
    try:
        server=SenseService()
        constructor.assert_called_once_with(str(directory),'auto',device='auto')
        server.destroy_node()
        server=None
        (directory/'model.safetensors').unlink()
        server=SenseService.__new__(SenseService)
        with pytest.raises(ValueError,match='incomplete'):
            server.__init__()
        assert constructor.call_count==1
    finally:
        if server is not None:
            server.destroy_node()
        rclpy.shutdown()


def test_legacy_vision_report_and_frozen_path(tmp_path):
    import json,yaml
    from test_cognition import local_model_directory
    from smpa_ros.model import resolve_vision_model
    root=tmp_path/'models'
    complete=local_model_directory(root/'vlm/complete',vision=True)
    (root/'legacy.yaml').write_text(yaml.safe_dump({'name':'legacy','model_id':'custom/vision'}))
    report=root/'download_report.json'
    report.write_text(json.dumps({'models':[{'repo_id':'custom/vision','local_dir':str(complete)}]}))
    config={'model':'legacy','vision':{'backend':'transformers'}}
    selected=resolve_vision_model(config,root,root)
    assert selected['path']==str(complete)
    report.write_text('{"models":[]}')
    with pytest.raises(ValueError,match='no local path'):
        resolve_vision_model(config,root,root)
    config['_run']={'stage_models':{'sense':{'path':str(complete),'model_id':'custom/vision'}}}
    assert resolve_vision_model(config,root,root)['path']==str(complete)


def test_shared_vlm_image_text_and_fresh_schema_requests(monkeypatch):
    """One weight load serves images and text, with no leaking token masks/settings."""
    torch, transformers, outlines = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', transformers)
    monkeypatch.setitem(sys.modules, 'outlines', outlines)
    monkeypatch.setitem(sys.modules, 'outlines.backends', outlines.backends)
    outlines.backends.get_json_schema_logits_processor.side_effect = [object(), object()]
    processor = transformers.AutoProcessor.from_pretrained.return_value
    tokenizer = processor.tokenizer
    inputs = {'input_ids': MagicMock(shape=(1, 3))}
    processor.return_value.to.return_value = inputs
    tokenizer.return_value.to.return_value = inputs
    processor.decode.return_value = '{"scene":"red"}'
    tokenizer.decode.side_effect = ['world facts', '{"action":"look_left"}']
    backend = TransformersVLM('/local/vlm', device='cpu')
    from smpa_ros.plan import ACTION_SCHEMA
    image = object()
    assert backend.describe(image, 'Sense', temperature=.2, seed=42, max_new_tokens=31,
        response_format='json_schema', schema={'type':'object'}) == '{"scene":"red"}'
    assert backend.generate('Model', temperature=.4, seed=43, max_new_tokens=37) == 'world facts'
    assert backend.generate('Plan', temperature=.1, seed=44, max_new_tokens=19,
        response_format='json_schema', schema=ACTION_SCHEMA) == '{"action":"look_left"}'
    transformers.AutoModelForImageTextToText.from_pretrained.assert_called_once()
    transformers.AutoModelForCausalLM.from_pretrained.assert_not_called()
    assert processor.call_args.kwargs['images'] == [image]
    assert tokenizer.call_count == 2  # Text requests never call the image processor.
    calls = backend.model.generate.call_args_list
    assert [call.kwargs['temperature'] for call in calls] == [.2, .4, .1]
    assert [call.kwargs['max_new_tokens'] for call in calls] == [31, 37, 19]
    assert [call.args[0] for call in torch.manual_seed.call_args_list] == [42, 43, 44]
    assert 'logits_processor' not in calls[1].kwargs
    assert calls[0].kwargs['logits_processor'][0] is not calls[2].kwargs['logits_processor'][0]
    assert all(call.args[0] is backend.model and call.args[1] is tokenizer
               for call in outlines.from_transformers.call_args_list)
