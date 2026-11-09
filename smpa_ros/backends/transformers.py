from contextlib import nullcontext
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image


class TransformersBackend:
    """Load a local causal language model once, retaining access to its internals."""

    def __init__(self, model_path: str, *, enable_thinking: bool | None = None):
        # Optional dependencies are loaded only when this backend is selected.
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.model_id = str(model_path)
        self.enable_thinking = enable_thinking
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, local_files_only=True,
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path, local_files_only=True,
        )
        self.model.eval()

    def generate(self, prompt: str | list[dict], *, max_new_tokens: int = 128, schema: dict | None = None, _conversation_settings=None) -> str:
        options = {}
        thinking = self.enable_thinking
        sample = False  # Preserve the SMPA Model/Plan path exactly.
        seed = None
        if _conversation_settings is not None:
            from smpa_ros.conversation import GENERATION_FIELDS, validate_generation
            supported = GENERATION_FIELDS - {'enable_thinking'}
            if getattr(self.model.config, 'model_type', None) == 'qwen3':
                supported = supported | {'enable_thinking'}
            controls = validate_generation(dict(_conversation_settings, max_new_tokens=max_new_tokens), supported)
            seed = controls.pop('seed', None)
            thinking = controls.pop('enable_thinking', thinking)
            sample = controls.pop('do_sample', False)
            controls.pop('max_new_tokens')
            inactive = []
            if not sample:
                for name in ('temperature', 'top_p', 'top_k'):
                    if name in controls:
                        inactive.append(name)
                        controls.pop(name)
            options.update(controls)
            defaults = self.model.generation_config.to_dict()
            effective = dict(defaults, **controls, max_new_tokens=max_new_tokens, do_sample=sample)
            if not sample:
                for name in ('temperature', 'top_p', 'top_k'):
                    effective.pop(name, None)
            effective.update(seed=seed, enable_thinking=thinking, inactive=inactive)
            self._last_generation_settings = effective
        if schema is not None:
            try:
                from outlines import from_transformers
                from outlines.backends import get_json_schema_logits_processor
                from jsonschema.validators import validator_for

                validator = validator_for(schema)(schema)
                validator.check_schema(schema)
                # Outlines 1.3.3 wraps these existing objects; it loads no weights.
                # Build a fresh stateful token mask for each generation request.
                wrapped = from_transformers(self.model, self.tokenizer)
                options['logits_processor'] = [get_json_schema_logits_processor(
                    'outlines_core', wrapped, json.dumps(schema),
                )]
            except Exception as error:
                raise RuntimeError('Cannot prepare Outlines JSON Schema constraints; run ./install.sh '
                                   'and check the schema. No unrestricted fallback is allowed: '
                                   f'{error}') from error
        messages = prompt if isinstance(prompt, list) else [{'role': 'user', 'content': prompt}]
        if self.tokenizer.chat_template:
            template_options = {} if thinking is None else {'enable_thinking': thinking}
            prompt = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, **template_options,
            )
            inputs = self.tokenizer(prompt, return_tensors='pt', add_special_tokens=False)
        else:
            if isinstance(prompt, list):
                raise ValueError('Conversational inference requires a model chat template')
            inputs = self.tokenizer(prompt, return_tensors='pt')
        inputs = inputs.to(self.model.device)
        # fork_rng restores the process RNG after this seeded invocation.
        devices = list(range(self.torch.cuda.device_count())) if seed is not None else []
        rng = self.torch.random.fork_rng(devices=devices) if seed is not None else nullcontext()
        with rng, self.torch.no_grad():
            if seed is not None:
                self.torch.manual_seed(seed)
            output = self.model.generate(
                **inputs, max_new_tokens=max_new_tokens, do_sample=sample, **options,
            )
        # Decode only the continuation, excluding the input prompt.
        text = self.tokenizer.decode(
            output[0, inputs['input_ids'].shape[-1]:], skip_special_tokens=True,
        )
        # Opt-in debugging records the actual template and raw continuation,
        # including EOS tokens, without changing persisted conversation messages.
        logger = logging.getLogger(__name__)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(json.dumps({
                'formatted_prompt': prompt,
                'raw_output': self.tokenizer.decode(
                    output[0, inputs['input_ids'].shape[-1]:], skip_special_tokens=False),
                'max_new_tokens': max_new_tokens, 'do_sample': sample,
            }, ensure_ascii=False))
        if schema is not None:
            try:
                validator.validate(json.loads(text))
            except Exception as error:
                raise RuntimeError('Structured generation did not produce a complete, schema-valid '
                                   f'JSON response (check max_new_tokens): {error}') from error
        return text


    def generate_conversation(self, prompt: str | list[dict], *, max_new_tokens: int = 128, **settings) -> dict:
        """Separate Qwen3's documented leading think block from its final answer.

        The existing template and enable_thinking setting are preserved. A trace
        is never requested or reconstructed here. Other model families retain
        their ordinary text, even if it happens to contain similar markup.
        """
        text = self.generate(prompt, max_new_tokens=max_new_tokens, _conversation_settings=settings)
        result = {'response': text}
        if getattr(self.model.config, 'model_type', None) == 'qwen3':
            stripped = text.lstrip()
            # Qwen can place the opening think delimiter in the generation prompt.
            if (getattr(self, '_last_generation_settings', {}).get('enable_thinking')
                    and not stripped.startswith('<think>') and '</think>' in stripped):
                stripped = '<think>' + stripped
            if stripped.startswith('<think>'):
                thinking, separator, response = stripped[len('<think>'):].partition('</think>')
                if not separator or not response.strip():
                    raise RuntimeError('Incomplete Qwen thinking block or missing final response; increase the output budget')
                result['response'] = response.strip()
                if thinking.strip():
                    result.update(thinking=thinking.strip(), reasoning_source='qwen3:<think>')
        effective = getattr(self, '_last_generation_settings', None)
        if isinstance(effective, dict):
            result['effective_settings'] = effective
        return result


class TransformersVLM:
    """CPU or single-GPU image inference from an already downloaded checkpoint."""

    def __init__(self, model_id: str, dtype: str = 'auto', *, device: str = 'auto') -> None:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        if device not in ('auto', 'cpu', 'cuda'):
            raise ValueError('device must be auto, cpu, or cuda')
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA was explicitly requested but is unavailable; check the NVIDIA '
                               'driver and PyTorch installation, or explicitly select cpu/auto.')
        if device == 'auto':
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        if dtype == 'auto':
            dtype = 'float16' if device == 'cuda' else 'float32'
        if dtype not in ('float32', 'float16', 'bfloat16'):
            raise ValueError('dtype must be auto, float32, float16, or bfloat16')
        if device == 'cpu' and dtype == 'float16':
            raise ValueError('CPU float16 is not supported by this backend; use float32/auto, '
                             'or explicitly select bfloat16 on suitable hardware.')
        if device == 'cpu' and dtype == 'bfloat16':
            import warnings
            warnings.warn('CPU bfloat16 may be slow or unsupported for some operations on this '
                          'hardware; use float32/auto if inference fails.', RuntimeWarning)
        if device == 'cuda' and dtype == 'bfloat16' and not torch.cuda.is_bf16_supported():
            raise RuntimeError('The selected CUDA device does not support bfloat16; use float16/auto.')
        device = 'cuda:0' if device == 'cuda' else 'cpu'
        print(f'Loading vision model on {device} with {dtype} (local weights only)', flush=True)
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(model_id, local_files_only=True)
        self.model = AutoModelForImageTextToText.from_pretrained(
            model_id, local_files_only=True, dtype=getattr(torch, dtype),
        ).to(device)
        self.model.eval()

    def describe(self, image: 'Image', prompt: str, *, max_new_tokens: int = 128) -> str:
        messages = [{'role': 'user', 'content': [
            {'type': 'image'}, {'type': 'text', 'text': prompt},
        ]}]
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        # BatchFeature.to casts floating image tensors only; integer token IDs stay integer.
        inputs = self.processor(
            text=text, images=[image], return_tensors='pt', add_special_tokens=False,
        ).to(self.model.device, dtype=self.model.dtype)
        with self.torch.no_grad():
            output = self.model.generate(
                **inputs, max_new_tokens=max_new_tokens, do_sample=False,
            )
        return self.processor.decode(
            output[0, inputs['input_ids'].shape[-1]:], skip_special_tokens=True,
        ).strip()
