from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image


class TransformersBackend:
    """Load a local causal language model once, retaining access to its internals."""

    def __init__(self, model_path: str):
        # Optional dependencies are loaded only when this backend is selected.
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, local_files_only=True,
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path, local_files_only=True,
        )
        self.model.eval()

    def generate(self, prompt: str, *, max_new_tokens: int = 128) -> str:
        messages = [{'role': 'user', 'content': prompt}]
        if self.tokenizer.chat_template:
            prompt = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
            )
            inputs = self.tokenizer(prompt, return_tensors='pt', add_special_tokens=False)
        else:
            inputs = self.tokenizer(prompt, return_tensors='pt')
        inputs = inputs.to(self.model.device)
        with self.torch.no_grad():
            output = self.model.generate(
                **inputs, max_new_tokens=max_new_tokens, do_sample=False,
            )
        # Decode only the continuation, excluding the input prompt.
        return self.tokenizer.decode(
            output[0, inputs['input_ids'].shape[-1]:], skip_special_tokens=True,
        )


class TransformersVLM:
    """Single-GPU image inference from an already downloaded HF checkpoint."""

    def __init__(self, model_id: str, dtype: str = 'float16') -> None:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        if dtype not in ('float16', 'bfloat16'):
            raise ValueError('dtype must be float16 or bfloat16')
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(model_id, local_files_only=True)
        self.model = AutoModelForImageTextToText.from_pretrained(
            model_id, local_files_only=True, dtype=getattr(torch, dtype),
        ).to('cuda:0')
        self.model.eval()

    def describe(self, image: 'Image', prompt: str, *, max_new_tokens: int = 128) -> str:
        messages = [{'role': 'user', 'content': [
            {'type': 'image'}, {'type': 'text', 'text': prompt},
        ]}]
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
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
