"""Load local language models and generate reproducible conversation candidates.

Concepts: the base model is an unchanged local weight directory. An adapter is a
separate PEFT checkpoint applied on top. A generation configuration records both
identities, hardware, decoding controls and candidate seeds for later feedback.
No model or executable remote code is downloaded by this module.
"""
import gc
from pathlib import Path


def hardware():
    """Describe CPU and CUDA availability without allocating a model.

    Inspect Torch for CUDA and bfloat16 support. Include free GPU memory when
    available; CPU remains available independently of CUDA. Return display metadata.
    """
    import torch
    import shutil
    info = {'cuda': torch.cuda.is_available(), 'cpu': True, 'bf16': False,
            'disk_free_gb': round(shutil.disk_usage('.').free / 2**30, 2)}
    if info['cuda']:
        info.update(gpu=torch.cuda.get_device_name(), bf16=torch.cuda.is_bf16_supported(),
                    gpu_free_gb=round(torch.cuda.mem_get_info()[0] / 2**30, 2))
    return info


def local_models(directory):
    """Find local model directories under the configured model root.

    A config file identifies a selectable directory, including nested model layouts.
    Exclude adapter configs and return resolved directory paths. Weight loading later
    reports missing or incompatible files without fetching replacements.
    """
    root = Path(directory).expanduser().resolve()
    return sorted({str(path.parent.resolve()) for path in root.rglob('config.json')})


def load_model(model_path, device='cpu', adapter=None, precision='float32', quantization=False, trainable=False):
    """Load an unchanged local base and optionally attach a compatible adapter.

    Validate explicit hardware and precision choices before loading. QLoRA requires
    CUDA and bitsandbytes and never falls back silently. Inference adapters load as
    frozen; training may request a trainable adapter. Return model and tokenizer;
    errors retain their original cause with a memory/compatibility hint.
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from peft import PeftConfig, PeftModel, prepare_model_for_kbit_training
    if device not in {'cpu', 'cuda'} or precision not in {'float32', 'bfloat16', 'float16'}:
        raise ValueError('Unsupported device or precision')
    info = hardware()
    if device == 'cuda' and not info['cuda']:
        raise ValueError('CUDA was requested but is unavailable')
    if device == 'cpu' and (precision != 'float32' or quantization):
        raise ValueError('CPU uses float32 LoRA; QLoRA and reduced precision require CUDA')
    if precision == 'bfloat16' and not info['bf16']:
        raise ValueError('This GPU does not support bfloat16')
    if quantization:
        import bitsandbytes  # noqa: F401
    dtype = getattr(torch, precision)
    options = {'local_files_only': True, 'trust_remote_code': False, 'torch_dtype': dtype}
    if quantization:
        options.update(quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                       bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype), device_map={'': 0})
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        if tokenizer.pad_token_id is None:
            tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelForCausalLM.from_pretrained(model_path, **options)
        if not quantization:
            model.to(device)
        if trainable and quantization:
            model = prepare_model_for_kbit_training(model)
        if adapter:
            config = PeftConfig.from_pretrained(adapter, local_files_only=True)
            if Path(config.base_model_name_or_path).resolve() != Path(model_path).resolve():
                raise ValueError('Adapter base model does not match the selected local model')
            model = PeftModel.from_pretrained(model, adapter, is_trainable=trainable, local_files_only=True)
        if not trainable:
            model.eval()
        return model, tokenizer
    except Exception as error:
        raise RuntimeError(f'Model load failed: {error}. Check local weights, adapter compatibility and free memory; try CPU or a smaller model.') from error


def generate(model, tokenizer, context, settings, seed=None):
    """Generate one response from the exact supplied conversational context.

    Render the model's chat template with an assistant generation prefix, then
    validate context capacity and seed Torch. Temperature zero means greedy decoding;
    positive temperature enables top-p sampling. Decode only newly generated tokens.
    Model errors propagate so the caller can retain the already saved user prompt.
    """
    import torch
    from transformers import set_seed
    set_seed(int(settings['seed'] if seed is None else seed))
    text = tokenizer.apply_chat_template(context, tokenize=False, add_generation_prompt=True)
    encoded = tokenizer(text, return_tensors='pt').to(model.device)
    maximum = int(settings['max_new_tokens'])
    if maximum < 1 or maximum > 8192:
        raise ValueError('Maximum new tokens must be between 1 and 8192')
    capacity = getattr(model.config, 'max_position_embeddings', 2048)
    if encoded.input_ids.shape[1] + maximum > capacity:
        raise ValueError('Conversation exceeds model context capacity; start a new conversation or reduce output length')
    temperature = float(settings['temperature'])
    top_p = float(settings['top_p'])
    if temperature < 0 or not 0 < top_p <= 1:
        raise ValueError('Temperature must be nonnegative and top-p must be in (0, 1]')
    options = {'max_new_tokens': maximum, 'do_sample': temperature > 0, 'pad_token_id': tokenizer.pad_token_id}
    if temperature > 0:
        options.update(temperature=temperature, top_p=top_p)
    with torch.inference_mode():
        result = model.generate(**encoded, **options)
    return tokenizer.decode(result[0, encoded.input_ids.shape[1]:], skip_special_tokens=True)


def release():
    """Collect discarded model objects and release unused CUDA allocations.

    Callers must first remove their model references. Garbage collection and CUDA
    cache clearing release unused memory without modifying saved weights.
    """
    import torch
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def model_information(model_path, adapter=None):
    """Snapshot local model configuration and artifact identity for reproducibility.

    Read configuration files as JSON and describe weight files by relative name,
    size and modification time. Record library versions as well. This lightweight
    manifest detects ordinary file replacement; it is not a cryptographic weight
    digest. Users must retain their original local weight directories unchanged.
    """
    import json
    from importlib.metadata import version
    result = {'libraries': {name: version(name) for name in ('torch', 'transformers', 'peft', 'trl')}}
    for key, directory in [('base', model_path), ('adapter', adapter)]:
        if directory:
            root = Path(directory)
            filenames = ('config.json', 'generation_config.json') if key == 'base' else ('adapter_config.json',)
            result[key] = {'path': str(root.resolve()), 'configuration': {
                filename: json.loads((root / filename).read_text()) for filename in filenames if (root / filename).exists()},
                'weights': [{'name': str(path.relative_to(root)), 'size': path.stat().st_size,
                             'modified_ns': path.stat().st_mtime_ns}
                            for pattern in ('*.safetensors', '*.bin') for path in root.rglob(pattern)]}
    return result
