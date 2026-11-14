import json
from contextlib import nullcontext

from smpa_ros.backends.base import generation_capabilities, validate_generation, validate_output
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image


def structured_output_instruction(schema, max_new_tokens):
    """Explain the requested JSON contract to the model before constrained decoding.

    The schema describes the complete response, and max_new_tokens is the existing
    output budget. A token mask enforces legal continuations but cannot tell the
    model which content to produce or encourage it to finish within that budget.
    Return a format instruction containing the unchanged schema so the model can
    plan a concise, complete response. This instruction does not replace either
    the token mask or validation of the final output.
    """
    return (
        'Answer the user request with actual observations or requested values, '
        'encoded as exactly one complete JSON value. Do not include reasoning, '
        'Markdown fences, or text before or after the JSON. Keep the answer concise '
        f'and finish the entire JSON within {max_new_tokens} output tokens.\n'
        'Required JSON Schema (a description of the response structure, not the answer):\n'
        + json.dumps(schema, ensure_ascii=False)
        + '\nReturn the answer data matching this schema. Do not copy the schema '
        'or describe its type. Use meaningful field names for the requested '
        'information unless the schema already specifies those names.'
    )


def validate_generated_output(text, continuation, max_new_tokens, response_format, schema):
    """Validate a decoded response and explain failures without repairing its content.

    Text is the complete decoded continuation; continuation contains its original
    token IDs. Apply the existing format and schema validator unchanged. Return
    valid output exactly as supplied. If validation fails, report the token count,
    whether the configured limit was reached, and a bounded representation of the
    rejected text's tail. Preserve the validator's original exception as the cause.
    Never accept a partial JSON value or silently retry with weaker constraints.
    """
    try:
        return validate_output(text, response_format, schema)
    except RuntimeError as error:
        token_count = len(continuation)
        stop = ('the output token limit was reached; the JSON may be incomplete'
                if token_count >= max_new_tokens else 'generation stopped before the output token limit')
        raise RuntimeError(
            f'{error}. Generated {token_count}/{max_new_tokens} tokens; {stop}. '
            f'Rejected output tail: {text[-500:]!r}'
        ) from error


def generation_options(model, tokenizer, temperature, schema):
    """Prepare independent sampling and schema masks for a single request."""
    options = {}
    if temperature is not None:
        options['do_sample'] = temperature > 0
        if temperature > 0:
            options['temperature'] = temperature
    if schema is not None:
        try:
            from outlines import from_transformers
            from outlines.backends import get_json_schema_logits_processor
            from jsonschema.validators import validator_for

            validator = validator_for(schema)(schema)
            validator.check_schema(schema)
            # Outlines 1.3.3 wraps these existing objects; it loads no weights.
            # Build a fresh stateful token mask for each generation request.
            wrapped = from_transformers(model, tokenizer)
            options['logits_processor'] = [get_json_schema_logits_processor(
                'outlines_core', wrapped, json.dumps(schema),
            )]
        except Exception as error:
            raise RuntimeError('Cannot prepare Outlines JSON Schema constraints; run ./install.sh '
                               'and check the schema. No unrestricted fallback is allowed: '
                               f'{error}') from error
    return options


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

    def generate(self, prompt: str, *, max_new_tokens: int = 128, schema: dict | None = None,
                 temperature=None, seed=None, reasoning_effort=None, response_format=None) -> str:
        """Generate text using the selected model and the requested output contract.

        Validate the decoding settings and create a fresh schema token mask when
        JSON is required. Keep the user's prompt intact and precede it with an
        explicit schema instruction for structured responses. Render the chat
        template when available; otherwise prepend that instruction to the plain
        prompt. Tokenize, generate within the unchanged output budget, and decode
        only the continuation. A supplied seed is scoped to this request. Return
        validated output or raise a diagnostic error without accepting malformed
        or truncated JSON. Plain-text requests receive no schema instruction.
        """
        response_format = response_format or ('json_schema' if schema is not None else 'text')
        if schema is not None and response_format != 'json_schema':
            raise ValueError('A schema requires response_format=json_schema')
        validate_generation(dict(temperature=temperature, max_output_tokens=max_new_tokens,
            seed=seed, reasoning_effort=reasoning_effort, response_format=response_format,
            output_schema=schema), backend='transformers')
        if response_format == 'json_object':
            schema = {'type': 'object'}
        options = generation_options(self.model, self.tokenizer, temperature, schema)
        messages = [{'role': 'user', 'content': prompt}]
        if schema is not None:
            messages.insert(0, {'role': 'system', 'content': structured_output_instruction(schema, max_new_tokens)})
        if self.tokenizer.chat_template:
            template_options = {} if self.enable_thinking is None else {'enable_thinking': self.enable_thinking}
            prompt = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, **template_options,
            )
            inputs = self.tokenizer(prompt, return_tensors='pt', add_special_tokens=False)
        else:
            if schema is not None:
                prompt = messages[0]['content'] + '\n\n' + prompt
            inputs = self.tokenizer(prompt, return_tensors='pt')
        inputs = inputs.to(self.model.device)
        # fork_rng restores CPU and all CUDA RNG states after this request.
        with self.torch.random.fork_rng() if seed is not None else nullcontext():
            if seed is not None:
                self.torch.manual_seed(seed)
            with self.torch.no_grad():
                output = self.model.generate(**inputs, max_new_tokens=max_new_tokens, **options)
        # Decode only the continuation, excluding the input prompt.
        continuation = output[0, inputs['input_ids'].shape[-1]:]
        text = self.tokenizer.decode(continuation, skip_special_tokens=True)
        return validate_generated_output(text, continuation, max_new_tokens, response_format, schema)


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
        self.tokenizer = self.processor.tokenizer
        self.enable_thinking = None

    # The text path uses the same full VLM and tokenizer; no decoder is reloaded.
    generate = TransformersBackend.generate

    def describe(self, image: 'Image', prompt: str, *, max_new_tokens: int = 128,
                 temperature=None, seed=None, reasoning_effort=None, response_format=None, schema=None) -> str:
        """Describe an image while enforcing the requested response format and schema.

        Validate settings and build a fresh token mask for structured output. The
        image and original user prompt remain together in the user message; an
        explicit system instruction supplies the unchanged JSON schema and output
        budget when needed. Render the processor's chat template and move image
        tensors to the loaded model's device and precision, preserving integer
        token IDs. Generate with the supplied controls and request-scoped seed,
        then decode only new tokens. Return a validated description, or report the
        rejected output and token-limit status without weakening schema enforcement.
        """
        response_format = response_format or ('json_schema' if schema is not None else 'text')
        if schema is not None and response_format != 'json_schema':
            raise ValueError('A schema requires response_format=json_schema')
        validate_generation(dict(temperature=temperature, max_output_tokens=max_new_tokens,
            seed=seed, reasoning_effort=reasoning_effort, response_format=response_format,
            output_schema=schema), backend='transformers',
            capabilities=generation_capabilities('transformers', vision=True))
        if response_format == 'json_object':
            schema = {'type': 'object'}
        options = generation_options(self.model, self.tokenizer, temperature, schema)
        messages = [{'role': 'user', 'content': [
            {'type': 'image'}, {'type': 'text', 'text': prompt},
        ]}]
        if schema is not None:
            messages.insert(0, {'role': 'system', 'content': structured_output_instruction(schema, max_new_tokens)})
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        # BatchFeature.to casts floating image tensors only; integer token IDs stay integer.
        inputs = self.processor(
            text=text, images=[image], return_tensors='pt', add_special_tokens=False,
        ).to(self.model.device, dtype=self.model.dtype)
        # fork_rng restores CPU and all CUDA RNG states after this request.
        with self.torch.random.fork_rng() if seed is not None else nullcontext():
            if seed is not None:
                self.torch.manual_seed(seed)
            with self.torch.no_grad():
                output = self.model.generate(**inputs, max_new_tokens=max_new_tokens, **options)
        continuation = output[0, inputs['input_ids'].shape[-1]:]
        text = self.processor.decode(continuation, skip_special_tokens=True).strip()
        return validate_generated_output(text, continuation, max_new_tokens, response_format, schema)
