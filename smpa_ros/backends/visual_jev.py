"""Thin adapter around upstream VDM; scoring stays in the official implementation."""

from pathlib import Path
import sys


class VisualJevBackend:
    def __init__(self, base_model: Path, adapter: Path, source_dir: Path,
                 device: str = 'cuda', max_pixels: int = 200704):
        import torch

        if device not in ('cpu', 'cuda'):
            raise ValueError('Supported devices are cpu and cuda; no automatic fallback')
        if max_pixels <= 0:
            raise ValueError('max_pixels must be positive')
        available_ram = next(int(line.split()[1]) * 1024 for line in
                             Path('/proc/meminfo').read_text().splitlines()
                             if line.startswith('MemAvailable:'))
        self.memory = {'available_ram_bytes': available_ram}
        if device == 'cuda':
            if not torch.cuda.is_available():
                raise RuntimeError('CUDA is unavailable; explicitly select device:=cpu if desired')
            free, total = torch.cuda.mem_get_info()
            self.memory.update(gpu_free_bytes=free, gpu_total_bytes=total)
            if free < 9 * 1024 ** 3:
                raise RuntimeError('Released BF16 inference needs about 8.4 GiB plus headroom; '
                                   'require 9 GiB free or explicitly use CPU. '
                                   'The upstream loader has no quantization option.')
        elif available_ram < 32 * 1024 ** 3:
            raise RuntimeError('CPU float32 inference requires 32 GiB available RAM '
                               'for headroom over the measured 26 GiB process peak')
        for directory, filename in ((base_model, 'config.json'), (adapter, 'adapter_config.json')):
            if not (directory / filename).is_file():
                raise FileNotFoundError(f'Missing {directory / filename}; download models first')
        if not (source_dir / 'vdm/models/vdm_model.py').is_file():
            raise FileNotFoundError(f'Official Visual Jev code not found under {source_dir}')
        sys.path.insert(0, str(source_dir.resolve()))
        from vdm.models.vdm_model import VDM
        from peft import PeftModel

        dtype = torch.bfloat16 if device == 'cuda' else torch.float32
        self.vdm = VDM(str(base_model.resolve()), device=device, dtype=dtype, with_heads=False)
        self.vdm.processor.image_processor.max_pixels = max_pixels
        self.vdm.backbone = PeftModel.from_pretrained(
            self.vdm.backbone, str(adapter.resolve()), local_files_only=True,
        ).eval()
        self.model = self.vdm.backbone
        self.last_output = None
        self.last_logits = None

    def evaluate(self, image, context: str, question: str, choices: list[str]) -> list[float]:
        import torch

        questions = [{'qtype': 'choice', 'instruction': question, 'candidates': choices}]
        with torch.inference_mode():
            group = self.vdm.prepare_group(image, questions, shared_context=context)
            self.last_output = self.vdm.run_independent(group)
            self.last_logits = self.last_output['lm_option_logits'][0, :group.n_options[0]]
            return torch.softmax(self.last_logits.float(), dim=-1).cpu().tolist()
