"""Local CLIPSeg loading and inference without ROS message or node ownership."""


def load_model(directory, device):
    """Load one local segmentation model and return its existing inference objects.

    The directory has already been checked by the caller. Validate requested
    CUDA availability, resolve automatic device selection, and load the local
    processor and float32 model once. Return Torch, processor, evaluated model,
    and resolved device; no download or background process is started.
    """
    import torch
    from transformers import CLIPSegForImageSegmentation, CLIPSegProcessor
    if device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA was requested but is unavailable; choose device:=cpu or fix CUDA')
    if device == 'auto':
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
    processor = CLIPSegProcessor.from_pretrained(str(directory), local_files_only=True)
    model = CLIPSegForImageSegmentation.from_pretrained(
        str(directory), local_files_only=True, dtype=torch.float32,
    ).to(device)
    model.eval()
    return torch, processor, model, device


def segment(torch, processor, model, image, threshold, *, prompt):
    """Return aligned soft scores and a binary mask using supplied inference objects.

    Resize the complete image to the model input square without cropping.
    Reuse the caller's processor and model in inference mode. Apply sigmoid
    and bilinear resizing to the original image size, reject nonfinite scores,
    and threshold only after resizing. Return tensors; ROS headers and message
    serialization remain the sensing adapter's responsibility.
    """
    size = model.config.vision_config.image_size
    inputs = processor(text=[prompt], images=image, padding=True,
                            return_tensors='pt', do_center_crop=False,
                            size={'height': size, 'width': size}).to(model.device)
    with torch.inference_mode():
        logits = model(**inputs).logits
        if logits.ndim != 3 or logits.shape[0] != 1:
            raise ValueError('CLIPSeg must return one two-dimensional score map')
        scores = logits.float().sigmoid().unsqueeze(1)
        scores = torch.nn.functional.interpolate(
            scores, size=(image.height, image.width), mode='bilinear', align_corners=False,
        )[0, 0].cpu().contiguous()
        if not torch.isfinite(scores).all().item():
            raise ValueError('CLIPSeg returned nonfinite scores')
        binary = (scores >= threshold).to(torch.uint8) * 255
    return scores, binary
