"""A local browser laboratory for conversation, human judgement and approved training.

The five views follow the researcher's workflow: converse, collect feedback, select
examples, approve an update, and inspect evidence. Models are local directories;
all durable application state comes from Markdown rather than browser session state.
"""
import os
from pathlib import Path
import sys

# Streamlit executes this file as a script; make the repository importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import streamlit as st
from rlhf_gym import inference, training, vault


def adapter_choices(root, model):
    """List completed compatible checkpoints without treating partial runs as adapters.

    Resolve artifact references relative to the vault and match the exact base model.
    Return an empty selection followed by immutable checkpoint paths for the controls.
    """
    return [None] + [str(Path(root) / event['checkpoint']) for event in vault.records(root, 'events')
                     if event.get('status') == 'completed' and event.get('base_model') == model
                     and (Path(root) / event['checkpoint'] / 'adapter_config.json').exists()]


def unload():
    """Discard this browser session's active inference model and clear unused memory.

    Remove both the model pair and its identity before collecting tensors. No vault
    records or saved checkpoints change when the user unloads a model.
    """
    st.session_state.pop('loaded', None)
    st.session_state.pop('loaded_configuration', None)
    inference.release()


def gym(root, model, adapter, device):
    """Let a human resume conversations, generate candidates and save judgements.

    Persist user input before inference. Snapshot the exact context and decoding
    controls with responses, so failures preserve the question and feedback remains
    attributable. Comparisons commit only the explicitly selected candidate to the
    conversation. Corrections are training examples, not edits to historic responses.
    """
    st.title('Conversation Gym')
    conversations = vault.records(root, 'conversations')
    if st.button('New / restart conversation'):
        created = vault.conversation(root)
        st.session_state['conversation'] = created['id']
        st.rerun()
    if not conversations:
        st.info('Create a conversation to begin. Select a local model and load it in the sidebar.')
        return
    ids = [item['id'] for item in conversations]
    if st.session_state.get('conversation') not in ids:
        st.session_state['conversation'] = ids[-1]
    selected = st.selectbox('Resume conversation', ids, key='conversation')
    with st.expander('Generation parameters', expanded=True):
        temperature = st.slider('Temperature', 0.0, 2.0, 0.7, 0.05)
        maximum = st.number_input('Maximum new tokens', 1, 8192, 256)
        top_p = st.slider('Top-p', 0.01, 1.0, 0.9, 0.01)
        seed = st.number_input('Seed', 0, 2147483646, 42)
        compare = st.checkbox('Generate A/B comparison')
    settings = dict(temperature=temperature, max_new_tokens=maximum, top_p=top_p, seed=seed)
    for message in vault.messages(root, selected):
        role = message['role']
        if role == 'comparison':
            with st.expander('Saved A/B comparison'):
                st.write({'A': message['candidates'][0], 'B': message['candidates'][1]})
        else:
            with st.chat_message(role):
                st.write(message['content'])
    loaded = st.session_state.get('loaded')
    identity = dict(model=model, adapter=adapter, device=device)
    active = st.session_state.get('loaded_configuration') == identity
    with st.form('prompt'):
        prompt = st.text_area('Message')
        sent = st.form_submit_button('Send', disabled=not active)
    history = vault.messages(root, selected)
    pending = bool(history and history[-1]['role'] == 'user')
    retry = st.button('Retry unanswered prompt', disabled=not (active and pending))
    unresolved = [message for message in history if message['role'] == 'comparison'
                  and not any(item.get('comparison_id') == message['id'] for item in history)]
    if (sent or retry) and unresolved:
        st.warning('Choose a response in the pending comparison before continuing.')
    elif sent or retry:
        if sent and not prompt.strip():
            st.warning('Enter a message.')
        elif sent and pending:
            st.warning('Retry the saved unanswered prompt or start a new conversation.')
        else:
            if sent:
                vault.append(root, 'messages', conversation_id=selected, role='user', content=prompt)
            context = vault.context_for(root, selected)
            configuration = dict(identity, generation=settings, seeds=[seed, seed + 1] if compare else [seed],
                                 artifacts=inference.model_information(model, adapter))
            with st.spinner('Generating locally…'):
                first = inference.generate(*loaded, context, settings)
                if compare:
                    second = inference.generate(*loaded, context, settings, seed=seed + 1)
                    vault.append(root, 'messages', conversation_id=selected, role='comparison', content='',
                                 candidates=[first, second], context=context, configuration=configuration)
                else:
                    vault.append(root, 'messages', conversation_id=selected, role='assistant', content=first,
                                 context=context, configuration=configuration)
            st.rerun()
    responses = [item for item in vault.messages(root, selected) if item['role'] in {'assistant', 'comparison'} and 'configuration' in item]
    if not responses:
        return
    st.subheader('Human feedback')
    response_id = st.selectbox('Response to evaluate', [item['id'] for item in responses])
    response = next(item for item in responses if item['id'] == response_id)
    with st.form('feedback_' + response_id):
        preferred = None
        if response['role'] == 'comparison':
            left, right = st.columns(2)
            left.write('A: ' + response['candidates'][0])
            right.write('B: ' + response['candidates'][1])
            choice = st.radio('Preferred response', ['No preference', 'A', 'B'], horizontal=True)
            preferred = {'No preference': None, 'A': 0, 'B': 1}[choice]
        else:
            st.write(response['content'])
        binary = st.radio('Binary feedback', ['Unrated', 'Thumbs up', 'Thumbs down'], horizontal=True)
        rating = st.select_slider('Rating', ['Unrated', 1, 2, 3, 4, 5])
        explanation = st.text_area('Written justification (metadata only)')
        correction = st.text_area('Corrected answer (SFT target)')
        if st.form_submit_button('Save feedback'):
            vault.feedback(root, response_id, rating=None if rating == 'Unrated' else rating,
                           binary={'Unrated': None, 'Thumbs up': True, 'Thumbs down': False}[binary],
                           explanation=explanation, correction=correction, preferred=preferred)
            history = vault.messages(root, selected)
            if preferred is not None and not any(item.get('comparison_id') == response_id for item in history):
                vault.append(root, 'messages', conversation_id=selected, role='assistant',
                             content=response['candidates'][preferred], comparison_id=response_id,
                             context=response['context'], configuration=response['configuration'])
            st.success('Feedback saved as a new immutable event.')
            st.rerun()
    existing = [event for event in vault.records(root, 'feedback') if event['response_id'] == response_id]
    with st.expander(f'Feedback history ({len(existing)})'):
        st.json(existing)


def training_view(root, model, adapter, device, threshold):
    """Display eligible examples, collect explicit approval and observe worker events.

    Selection is manual; notifications never start training. Continuing an adapter
    requires choosing it explicitly. Snapshot chosen examples before starting the
    independent worker, then expose its metrics, errors and cooperative stop control.
    """
    st.title('Training')
    if not model:
        st.info('Select a locally available model first.')
        return
    counts = vault.notifications(root, model, threshold)
    st.write(counts)
    for method, count in counts.items():
        if count['ready']:
            st.info(f"{method}: {count['count']} new examples reached the threshold. Review and approve a run when ready.")
    algorithm = st.radio('Algorithm', ['SFT', 'DPO'], horizontal=True)
    replay = st.checkbox('Replay previously consumed examples', value=False)
    eligible = vault.examples(root, algorithm, model, replay)
    st.caption('Corrections train SFT. Explicit chosen/rejected pairs train DPO. Scores and explanations remain metadata.')
    selected = st.multiselect('Training examples', [entry['id'] for entry in eligible], default=[entry['id'] for entry in eligible])
    with st.expander('Preview selected dataset'):
        st.json([entry for entry in eligible if entry['id'] in selected])
    with st.form('training_configuration'):
        mode = st.radio('Adapter update', ['Start new adapter', 'Continue selected adapter'])
        st.caption('When continuing, saved adapter rank, alpha and dropout are retained. The LoRA fields below apply to new adapters.')
        st.write(f'Base model: {model}')
        st.write(f'Parent adapter: {adapter or "none"}')
        learning_rate = st.number_input('Learning rate', 0.0000001, 0.1, 0.0001, format='%.7f')
        epochs = st.number_input('Epochs', 0.1, 100.0, 1.0)
        batch_size = st.number_input('Batch size', 1, 128, 1)
        accumulation = st.number_input('Gradient accumulation', 1, 128, 4)
        rank = st.number_input('LoRA rank', 1, 256, 8)
        alpha = st.number_input('LoRA alpha', 1, 512, 16)
        dropout = st.slider('LoRA dropout', 0.0, 0.9, 0.05)
        length = st.number_input('Maximum training sequence length', 32, 8192, 512)
        precision = st.selectbox('Precision', ['float32'] if device == 'cpu' else ['float32', 'bfloat16', 'float16'])
        quantization = st.checkbox('QLoRA (CUDA + bitsandbytes required)', disabled=device == 'cpu')
        approve = st.checkbox('I approve training on exactly these selected examples with this configuration.')
        start = st.form_submit_button('Start approved training', disabled=not selected)
    if start:
        if mode == 'Continue selected adapter' and not adapter:
            raise ValueError('Select an adapter in the sidebar before continuing it')
        config = dict(model=model, algorithm=algorithm, device=device, adapter=adapter if mode.startswith('Continue') else None,
                      learning_rate=learning_rate, epochs=epochs, batch_size=batch_size, gradient_accumulation=accumulation,
                      rank=rank, alpha=alpha, dropout=dropout, max_length=length, precision=precision, quantization=quantization)
        run = training.approve_run(root, config, selected, approved=approve, replay=replay)
        unload()
        training.start_worker(root, run)
        st.success(f"Started approved run {run['id']}")
    show_runs(root)


@st.fragment(run_every='3s')
def show_runs(root):
    """Refresh training status and metric tables while workers run independently.

    Each run reads its append-only events. Stop controls create cooperative requests;
    errors and checkpoint references stay visible after restart.
    """
    st.subheader('Training runs')
    for run in reversed(vault.records(root, 'training')):
        status = training.run_status(root, run['id'])
        with st.expander(f"{run['algorithm']} · {run['id'][:10]} · {status['status']}", expanded=status['status'] == 'running'):
            st.json(status)
            if status['status'] in {'approved', 'queued', 'running'} and st.button('Request stop', key='stop_' + run['id']):
                training.request_stop(root, run['id'])
            metrics = [dict(item['values'], step=item['step']) for item in vault.records(root, 'metrics') if item['run_id'] == run['id']]
            if metrics:
                st.dataframe(metrics)


def models_view(root, models, model, adapter):
    """Show available hardware, local models and compatible completed adapters.

    Report the actual loaded session identity separately from selector choices so
    users can see whether they need to load their newly selected configuration.
    """
    st.title('Models')
    st.json(inference.hardware())
    st.write('Available local models', models)
    st.write('Active inference configuration', st.session_state.get('loaded_configuration', 'Unloaded'))
    st.write('Selected model', model)
    st.write('Selected adapter', adapter or 'Base model')
    st.write('Compatible immutable adapters', adapter_choices(root, model)[1:])


def experiments_view(root):
    """Expose saved conversations, datasets, configurations and checkpoint lineage.

    Show the original Markdown text and offer downloads so experiment records remain
    inspectable without a special viewer or an independent database.
    """
    st.title('Experiments')
    for kind in ('conversations', 'training', 'feedback', 'events'):
        with st.expander(kind.title()):
            for record in reversed(vault.records(root, kind)):
                path = Path(root) / kind / (record['id'] + '.md')
                st.caption(str(path))
                st.download_button(record.get('title', record['id']), path.read_text(), file_name=path.name,
                                   key='download_' + kind + record['id'])
                st.json(record, expanded=False)


def metrics_view(root):
    """Plot recorded optimization measurements and human feedback independently.

    Use step as the training axis and preserve run identity for comparison. Feedback
    charts describe collected judgements; neither falling loss nor ratings establish
    that an adapter is qualitatively more helpful.
    """
    import pandas as pd
    import psutil
    st.title('Metrics')
    st.caption('Training loss measures optimization. Humans must evaluate qualitative improvement.')
    st.json(dict(inference.hardware(), ram_used_gb=round(psutil.virtual_memory().used / 2**30, 2),
                 ram_available_gb=round(psutil.virtual_memory().available / 2**30, 2)))
    metric_records = vault.records(root, 'metrics')
    run_ids = sorted({item['run_id'] for item in metric_records})
    selected = st.multiselect('Compare training runs', run_ids, default=run_ids)
    for metric in ('loss', 'learning_rate', 'epoch', 'rewards/chosen', 'rewards/rejected'):
        rows = [{'step': item['step'], 'run': item['run_id'], 'value': item['values'][metric]}
                for item in metric_records if item['run_id'] in selected and metric in item['values']]
        if rows:
            st.write(metric)
            frame = pd.DataFrame(rows).pivot_table(index='step', columns='run', values='value', aggfunc='last')
            st.line_chart(frame)
    feedback = vault.records(root, 'feedback')
    if feedback:
        frame = pd.DataFrame(feedback)
        st.write('Binary feedback distribution')
        st.bar_chart(frame['binary'].dropna().astype(str).value_counts())
        st.write('Numerical rating history')
        rated = frame.dropna(subset=['rating'])
        if not rated.empty:
            st.line_chart(rated.set_index('created')['rating'])
        st.write('A/B selections (0 = A, 1 = B)')
        st.bar_chart(frame['preferred'].dropna().astype(str).value_counts())


def main():
    """Configure the local workspace and route to one of five laboratory views.

    Environment variables supply initial vault and model roots. Sidebar controls
    choose inference identity without loading weights automatically. Errors are
    displayed in the browser while existing vault records remain intact.
    """
    st.set_page_config(page_title='Local RLHF Training Gym', page_icon='🧪', layout='wide')
    st.markdown('<style>.block-container{max-width:1200px}h1{letter-spacing:-.04em}section[data-testid="stSidebar"]{border-right:1px solid #343d48}</style>', unsafe_allow_html=True)
    with st.sidebar:
        st.header('🧪 Local RLHF Gym')
        root = str(Path(st.text_input('Obsidian vault', os.environ.get('RLHF_VAULT', 'RLHF-Vault'))).expanduser().resolve())
        directory = st.text_input('Local model directory', os.environ.get('RLHF_MODELS', 'models/llm'))
        threshold = st.number_input('Feedback notification threshold', 1, 100000, 10)
        view = st.radio('Workspace', ['Conversation Gym', 'Training', 'Models', 'Experiments', 'Metrics'])
        models = inference.local_models(directory)
        default = next((i for i, item in enumerate(models) if '0.6b' in item.lower()), 0)
        model = st.selectbox('Base model', models, index=default) if models else None
        adapter = st.selectbox('Adapter', adapter_choices(root, model), format_func=adapter_label)
        device = st.selectbox('Device', ['cpu', 'cuda'])
        identity = dict(model=model, adapter=adapter, device=device)
        if st.button('Load model', disabled=not model):
            unload()
            with st.spinner('Loading local weights…'):
                st.session_state['loaded'] = inference.load_model(model, device, adapter)
                st.session_state['loaded_configuration'] = identity
        if st.button('Unload model'):
            unload()
        st.caption('Active: ' + str(st.session_state.get('loaded_configuration', 'Unloaded')))
        st.caption('Local files only · Explicit training approval · Immutable history')
    if view == 'Conversation Gym':
        gym(root, model, adapter, device)
    elif view == 'Training':
        training_view(root, model, adapter, device, threshold)
    elif view == 'Models':
        models_view(root, models, model, adapter)
    elif view == 'Experiments':
        experiments_view(root)
    else:
        metrics_view(root)


def adapter_label(adapter):
    """Render a checkpoint selector entry in terms of its run identity.

    A missing adapter means the original base policy. Saved checkpoints display their
    parent run directory, which stays stable when more checkpoints are created.
    """
    return 'Base model (no adapter)' if adapter is None else Path(adapter).parent.name


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        st.error(str(error))
        st.exception(error)
