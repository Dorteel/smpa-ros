"""Verify immutable recovery, dataset semantics, approval and real CPU training.

The tiny local GPT-2 fixture has random weights: it proves optimizer/checkpoint
plumbing, not language quality. No test downloads a model or writes to a user vault.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from rlhf_gym import vault, training, inference


def saved_response(root, model, comparison=False):
    """Save an original user context and a response with exact model provenance.

    Create a new conversation and persist the question before a synthetic response.
    The response is only a fixture for feedback tests; genuine inference is verified
    separately with local random model weights.
    """
    conversation = vault.conversation(root, 'Test conversation')
    vault.append(root, 'messages', conversation_id=conversation['id'], role='user', content='Hello')
    return vault.append(root, 'messages', conversation_id=conversation['id'],
                        role='comparison' if comparison else 'assistant', content='' if comparison else 'Hello human',
                        candidates=['Good answer', 'Bad answer'] if comparison else None,
                        context=vault.context_for(root, conversation['id']),
                        configuration={'model': str(model), 'adapter': None, 'generation': {'seed': 42}})


def test_recovery_and_exclusive_writes(tmp_path):
    """Recover Unicode Markdown and reject attempts to overwrite its identity.

    Save a response with multiline frontmatter-like content, reread it from disk,
    then attempt a duplicate publication. The original remains byte-for-byte intact.
    """
    response = saved_response(tmp_path, '/model')
    event = vault.feedback(tmp_path, response['id'], explanation='こんにちは\n---\nGood', correction='Yes\nNo')
    path = tmp_path / 'feedback' / (event['id'] + '.md')
    before = path.read_bytes()
    assert vault.records(tmp_path, 'feedback')[0] == event
    with pytest.raises(FileExistsError):
        vault.write_record(tmp_path, 'feedback', dict(event, explanation='changed'))
    assert path.read_bytes() == before
    assert vault.context_for(tmp_path, response['conversation_id']) == [
        {'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Hello human'}]


def test_feedback_semantics_and_notifications(tmp_path):
    """Keep scalar ratings separate from SFT corrections and DPO pairs.

    First save metadata-only feedback and check that it creates no examples. Add
    two corrections and an explicit preference, preserving all events and their
    original prompt context. Counts can notify without producing any training run.
    """
    response = saved_response(tmp_path, '/model')
    vault.feedback(tmp_path, response['id'], binary=True, rating=5, explanation='Excellent')
    assert not vault.examples(tmp_path, 'SFT', '/model')
    assert not vault.examples(tmp_path, 'DPO', '/model')
    for correction in ['Better answer', 'Another correction']:
        vault.feedback(tmp_path, response['id'], correction=correction)
    pair = saved_response(tmp_path, '/model', True)
    vault.feedback(tmp_path, pair['id'], preferred=1)
    assert len(vault.records(tmp_path, 'feedback')) == 4
    sft = vault.examples(tmp_path, 'SFT', '/model')
    dpo = vault.examples(tmp_path, 'DPO', '/model')
    assert sft[0]['row']['prompt'] == response['context']
    assert dpo[0]['row']['chosen'][0]['content'] == 'Bad answer'
    assert dpo[0]['row']['rejected'][0]['content'] == 'Good answer'
    assert vault.notifications(tmp_path, '/model', 2) == {'SFT': {'count': 2, 'ready': True}, 'DPO': {'count': 1, 'ready': False}}
    assert vault.records(tmp_path, 'training') == []
    assert not vault.examples(tmp_path, 'SFT', '/another-model')
    with pytest.raises(ValueError):
        vault.feedback(tmp_path, response['id'], preferred=0)
    with pytest.raises(ValueError):
        vault.feedback(tmp_path, response['id'], rating=6)


def test_approval_and_consumption(tmp_path):
    """Reject unapproved execution and consume only successful selected examples.

    A failed run must leave feedback eligible. A completion event consumes the exact
    snapshot for its method, while explicit replay makes it selectable again.
    """
    model = tmp_path / 'model'
    model.mkdir()
    response = saved_response(tmp_path, model)
    event = vault.feedback(tmp_path, response['id'], correction='Better')
    config = {'model': str(model)}
    with pytest.raises(PermissionError):
        training.approve_run(tmp_path, config, [event['id']])
    with pytest.raises(PermissionError):
        training.execute(tmp_path, 'nonexistent')
    with pytest.raises(PermissionError):
        training.start_worker(tmp_path, {'id': 'fake', 'approved': True})
    run = training.approve_run(tmp_path, config, [event['id']], approved=True)
    vault.append(tmp_path, 'events', run_id=run['id'], status='failed')
    assert len(vault.examples(tmp_path, 'SFT', str(model))) == 1
    vault.append(tmp_path, 'events', run_id=run['id'], status='completed')
    assert not vault.examples(tmp_path, 'SFT', str(model))
    assert len(vault.examples(tmp_path, 'SFT', str(model), replay=True)) == 1
    with pytest.raises(ValueError):
        training.approve_run(tmp_path, config, [event['id']], approved=True)


@pytest.fixture
def tiny_model(tmp_path):
    """Construct a real, tiny causal model and tokenizer without network access.

    A word-level vocabulary and explicit chat template make conversational datasets
    executable. Save random GPT-2 weights locally so both TRL trainers and PEFT load
    through exactly the same APIs as user-supplied pretrained language models.
    """
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast
    directory = tmp_path / 'tiny-model'
    tokenizer = Tokenizer(WordLevel({'<pad>': 0, '<eos>': 1, '<unk>': 2, 'user': 3, 'assistant': 4,
                                    'Hello': 5, 'Good': 6, 'Bad': 7, 'answer': 8, 'Better': 9, ':': 10}, unk_token='<unk>'))
    tokenizer.pre_tokenizer = Whitespace()
    wrapped = PreTrainedTokenizerFast(tokenizer_object=tokenizer, pad_token='<pad>', eos_token='<eos>', unk_token='<unk>')
    wrapped.chat_template = "{% for message in messages %}{{ message['role'] + ': ' + message['content'] + ' ' + eos_token + ' ' }}{% endfor %}{% if add_generation_prompt %}{{ 'assistant: ' }}{% endif %}"
    model = GPT2LMHeadModel(GPT2Config(vocab_size=11, n_positions=128, n_ctx=128, n_embd=16, n_layer=1, n_head=2,
                                      eos_token_id=1, pad_token_id=0, bos_token_id=1))
    model.save_pretrained(directory)
    wrapped.save_pretrained(directory)
    return directory


@pytest.mark.parametrize('algorithm', ['SFT', 'DPO'])
def test_real_training_checkpoint_and_continuation(tmp_path, tiny_model, algorithm):
    """Run real CPU optimization, reload the adapter and continue an immutable child.

    Build a valid correction or explicit preference, approve one optimizer step,
    and execute the actual TRL trainer. Verify weights changed, reload for inference,
    then collect a fresh example and continue from the saved adapter. The parent's
    bytes remain unchanged, and prior examples stay excluded unless replay is chosen.
    """
    import torch
    from safetensors.torch import load_file
    root = tmp_path / 'vault'
    response = saved_response(root, tiny_model, algorithm == 'DPO')
    event = vault.feedback(root, response['id'], correction='Better answer' if algorithm == 'SFT' else '',
                           preferred=0 if algorithm == 'DPO' else None)
    config = dict(model=str(tiny_model), algorithm=algorithm, max_steps=1, max_length=64, gradient_accumulation=1, rank=2, alpha=4)
    run = training.approve_run(root, config, [event['id']], approved=True)
    training.execute(root, run['id'])
    status = training.run_status(root, run['id'])
    assert status['status'] == 'completed'
    assert status['steps'] == 1
    checkpoint = root / status['checkpoint']
    weights_path = checkpoint / 'adapter_model.safetensors'
    original_bytes = weights_path.read_bytes()
    weights = load_file(weights_path)
    assert any(torch.count_nonzero(value).item() for name, value in weights.items() if 'lora_B' in name)
    model, tokenizer = inference.load_model(str(tiny_model), adapter=str(checkpoint))
    assert isinstance(inference.generate(model, tokenizer, response['context'], dict(seed=1, temperature=0, top_p=1, max_new_tokens=2)), str)
    del model
    second = vault.feedback(root, response['id'], correction='Good answer' if algorithm == 'SFT' else '',
                            preferred=1 if algorithm == 'DPO' else None)
    assert [item['id'] for item in vault.examples(root, algorithm, str(tiny_model))] == [second['id']]
    child = training.approve_run(root, dict(config, adapter=str(checkpoint)), [second['id']], approved=True)
    training.execute(root, child['id'])
    assert training.run_status(root, child['id'])['status'] == 'completed'
    assert weights_path.read_bytes() == original_bytes
    assert len(vault.records(root, 'metrics')) >= 2


@pytest.mark.parametrize('cancel', [True, False])
def test_worker_subprocess_and_cancel(tmp_path, tiny_model, cancel):
    """Run an approved worker separately and honor a stop requested before loading.

    Optionally persist a stop request, then launch the subprocess used by the
    dashboard. A cancelled job keeps its example eligible; a completed job saves a
    real adapter and consumes its example. Repeated launch attempts are rejected.
    """
    import time
    response = saved_response(tmp_path, tiny_model)
    event = vault.feedback(tmp_path, response['id'], correction='Better answer')
    run = training.approve_run(tmp_path, dict(model=str(tiny_model), max_steps=1, max_length=64, gradient_accumulation=1), [event['id']], approved=True)
    if cancel:
        training.request_stop(tmp_path, run['id'])
    pid = training.start_worker(tmp_path, run)
    for _ in range(120):
        status = training.run_status(tmp_path, run['id'])
        if status['status'] in {'cancelled', 'completed', 'failed', 'interrupted'}:
            break
        time.sleep(0.25)
    expected_status = 'cancelled' if cancel else 'completed'
    if status['status'] != expected_status:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    os.waitpid(pid, 0)
    assert status['status'] == expected_status, status
    with pytest.raises(ValueError):
        training.start_worker(tmp_path, run)
    assert bool(vault.examples(tmp_path, 'SFT', str(tiny_model))) is cancel


def test_browser_views(tmp_path, monkeypatch):
    """Exercise the real Streamlit widget tree without loading a user model.

    Use temporary model and vault roots, create a conversation through the UI, then
    visit each view and verify it renders without an exception. Streamlit AppTest
    validates Python/widget interaction, not a full browser's layout or rendering.
    """
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('RLHF_VAULT', str(tmp_path / 'vault'))
    monkeypatch.setenv('RLHF_MODELS', str(tmp_path / 'models'))
    app = AppTest.from_file('rlhf_gym/app.py', default_timeout=30).run()
    assert not app.exception
    next(button for button in app.button if button.label == 'New / restart conversation').click().run()
    assert not app.exception
    assert len(vault.records(tmp_path / 'vault', 'conversations')) == 1
    for view in ['Training', 'Models', 'Experiments', 'Metrics', 'Conversation Gym']:
        app.sidebar.radio[0].set_value(view).run()
        assert not app.exception


def test_dashboard_conversation_feedback_and_preference(tmp_path, tiny_model, monkeypatch):
    """Exercise conversation and judgement controls with deterministic model calls.

    Model loading and generation are substituted in this widget test; the separate
    real training tests cover those libraries. Persist a generated answer, written
    feedback, correction and A/B choice through actual forms, then create a fresh
    app session and recover the same conversation from Markdown.
    """
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('RLHF_VAULT', str(tmp_path / 'vault'))
    monkeypatch.setenv('RLHF_MODELS', str(tiny_model.parent))
    monkeypatch.setattr(inference, 'load_model', fake_load)
    monkeypatch.setattr(inference, 'generate', fake_generate)
    app = AppTest.from_file('rlhf_gym/app.py', default_timeout=30).run()
    click(app, 'Load model')
    click(app, 'New / restart conversation')
    next(item for item in app.text_area if item.label == 'Message').set_value('Hello')
    click(app, 'Send')
    next(item for item in app.radio if item.label == 'Binary feedback').set_value('Thumbs up')
    next(item for item in app.select_slider if item.label == 'Rating').set_value(5)
    next(item for item in app.text_area if item.label.startswith('Written justification')).set_value('Clear explanation')
    next(item for item in app.text_area if item.label.startswith('Corrected answer')).set_value('Better answer')
    click(app, 'Save feedback')
    root = tmp_path / 'vault'
    assert vault.records(root, 'feedback')[0]['explanation'] == 'Clear explanation'
    assert len(vault.examples(root, 'SFT', str(tiny_model))) == 1
    next(item for item in app.checkbox if item.label == 'Generate A/B comparison').check()
    next(item for item in app.text_area if item.label == 'Message').set_value('Another question')
    click(app, 'Send')
    pair = [item for item in vault.records(root, 'messages') if item['role'] == 'comparison'][0]
    next(item for item in app.selectbox if item.label == 'Response to evaluate').set_value(pair['id']).run()
    next(item for item in app.radio if item.label == 'Preferred response').set_value('B')
    click(app, 'Save feedback')
    assert len(vault.examples(root, 'DPO', str(tiny_model))) == 1
    assert vault.context_for(root, pair['conversation_id'])[-1]['content'] == 'Candidate 43'
    fresh = AppTest.from_file('rlhf_gym/app.py', default_timeout=30).run()
    assert not fresh.exception
    assert len(fresh.chat_message) == 4
    fresh.sidebar.radio[0].set_value('Metrics').run()
    assert not fresh.exception
    fresh.sidebar.radio[0].set_value('Training').run()
    assert not fresh.exception
    click_button = next(button for button in fresh.button if button.label == 'Start approved training')
    click_button.click().run()
    assert vault.records(root, 'training') == []
    assert any('Explicit training approval' in item.value for item in fresh.error)
    next(item for item in fresh.checkbox if item.label.startswith('I approve')).check()
    monkeypatch.setattr(training, 'start_worker', approved_worker_fixture)
    click(fresh, 'Start approved training')
    assert len(vault.records(root, 'training')) == 1


def fake_load(*args, **kwargs):
    """Return placeholders for the widget test without allocating model weights.

    The interface receives the same two objects as a real loader; generation is
    separately substituted in that test, so these objects never enter Torch.
    """
    return object(), object()


def fake_generate(model, tokenizer, context, settings, seed=None):
    """Return a seed-labelled candidate so A/B feedback is deterministic.

    Preserve the generation call signature used by the app. The explicit comparison
    seed produces a distinct rejected/chosen answer without stochastic test failures.
    """
    return 'Candidate ' + str(settings['seed'] if seed is None else seed)


def click(app, label):
    """Activate a named Streamlit button and assert that its rerun succeeds.

    Labels express the user action and avoid depending on incidental widget ordering.
    Return the updated app so callers can continue the same browser session.
    """
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception
    return app


def test_pinned_environment():
    """Check the declared direct dependencies against the running interpreter.

    Parse the exact pins in the gym requirements, accepting Torch's CPU/CUDA local
    build suffix. A mismatch fails with the package identity rather than silently
    testing a different trainer stack from the documented installation.
    """
    from importlib.metadata import version
    from packaging.specifiers import SpecifierSet
    for line in Path('requirements-rlhf.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            name, expected = line.split('==')
            assert version(name) in SpecifierSet('==' + expected), (name, version(name), expected)


def test_hardware_rejections(tmp_path):
    """Reject unsupported CPU precision and QLoRA before loading any weight files.

    Point at an empty directory so success cannot be explained by a real model load.
    Explicit incompatible choices raise and never silently switch hardware modes.
    """
    with pytest.raises(ValueError, match='CPU uses float32'):
        inference.load_model(str(tmp_path), device='cpu', precision='float16')
    with pytest.raises(ValueError, match='CPU uses float32'):
        inference.load_model(str(tmp_path), device='cpu', quantization=True)


def test_failed_inference_preserves_prompt(tmp_path, tiny_model, monkeypatch):
    """Preserve a user prompt across generation failure and allow an explicit retry.

    Substitute a failing generator in the real widget workflow, verify the question
    survives on disk, then restore deterministic generation and retry it without
    appending a duplicate question. The exception is displayed by the application.
    """
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('RLHF_VAULT', str(tmp_path / 'vault'))
    monkeypatch.setenv('RLHF_MODELS', str(tiny_model.parent))
    monkeypatch.setattr(inference, 'load_model', fake_load)
    monkeypatch.setattr(inference, 'generate', failed_generate)
    app = AppTest.from_file('rlhf_gym/app.py', default_timeout=30).run()
    click(app, 'Load model')
    click(app, 'New / restart conversation')
    next(item for item in app.text_area if item.label == 'Message').set_value('Keep this question')
    next(button for button in app.button if button.label == 'Send').click().run()
    root = tmp_path / 'vault'
    assert len(vault.records(root, 'messages')) == 1
    assert vault.records(root, 'messages')[0]['content'] == 'Keep this question'
    monkeypatch.setattr(inference, 'generate', fake_generate)
    app.run()
    click(app, 'Retry unanswered prompt')
    assert len(vault.records(root, 'messages')) == 2


def failed_generate(*args, **kwargs):
    """Raise an inference error to exercise the persisted-question failure path.

    Ignore model arguments deliberately. This fixture represents an allocation or
    decoding failure after the application has already written the human's message.
    """
    raise RuntimeError('Simulated model memory failure')


def approved_worker_fixture(root, run):
    """Check the UI's approved snapshot without starting an expensive widget job.

    Worker execution is verified separately with genuine CPU training. This fixture
    proves the UI passes persisted approval and the selected dataset to its launcher.
    """
    assert run['approved'] is True
    assert run['dataset']
    assert any(item['id'] == run['id'] for item in vault.records(root, 'training'))
    return 0
