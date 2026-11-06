"""Local BLIP image captioning used to give text-only chat models visual context."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

_CACHE = {"model": None, "processor": None, "failed": False}
_VQA = {"model": None, "processor": None, "failed": False}
_DEVICE_CACHE = {}
_VQA_DEVICE_CACHE = {}


def _model_dir() -> Path | None:
    candidates = []
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        candidates.append(Path(ai_home) / "models" / "vision" / "blip-image-captioning-base")
    candidates.append(Path(__file__).resolve().parent.parent / "ai" / "models" / "vision" / "blip-image-captioning-base")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "_ai_install" / "TwilightHarnessPortable" / "ai" / "models" / "vision" / "blip-image-captioning-base")
    for candidate in candidates:
        if (candidate / "config.json").is_file():
            return candidate
    return None


def _load():
    if _CACHE["failed"]:
        return None, None
    if _CACHE["model"] is not None:
        return _CACHE["model"], _CACHE["processor"]
    path = _model_dir()
    if path is None:
        _CACHE["failed"] = True
        return None, None
    try:
        import torch
        from transformers import BlipForConditionalGeneration, BlipProcessor
        processor = BlipProcessor.from_pretrained(str(path), local_files_only=True)
        model = BlipForConditionalGeneration.from_pretrained(str(path), local_files_only=True)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = model.to(device).eval()
        _CACHE.update(model=model, processor=processor)
        return model, processor
    except Exception:
        _CACHE["failed"] = True
        return None, None


def _load_vqa():
    if _VQA["failed"]:
        return None, None
    if _VQA["model"] is not None:
        return _VQA["model"], _VQA["processor"]
    candidates = []
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        candidates.append(Path(ai_home) / "models" / "vision" / "blip-vqa-base")
    candidates.append(Path(__file__).resolve().parent.parent / "ai" / "models" / "vision" / "blip-vqa-base")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "_ai_install" / "TwilightHarnessPortable" / "ai" / "models" / "vision" / "blip-vqa-base")
    path = next((item for item in candidates if (item / "config.json").is_file()), None)
    if path is None:
        _VQA["failed"] = True
        return None, None
    try:
        import torch
        from transformers import BlipForQuestionAnswering, BlipProcessor
        processor = BlipProcessor.from_pretrained(str(path), local_files_only=True)
        model = BlipForQuestionAnswering.from_pretrained(str(path), local_files_only=True)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _VQA.update(model=model.to(device).eval(), processor=processor)
        return _VQA["model"], _VQA["processor"]
    except Exception:
        _VQA["failed"] = True
        return None, None


def _open_image(path: str | Path | None, data_uri: str):
    from PIL import Image
    import io
    if path:
        return Image.open(path).convert("RGB")
    if data_uri:
        return Image.open(io.BytesIO(base64.b64decode(data_uri.split(",", 1)[-1]))).convert("RGB")
    return None


def answer(question: str, path: str | Path | None = None, data_uri: str = "") -> str:
    """Local visual question answering (BLIP VQA), no remote vision API."""
    model, processor = _load_vqa()
    if model is None:
        return ""
    try:
        import torch
        image = _open_image(path, data_uri)
        if image is None:
            return ""
        inputs = processor(images=image, text=(question or "Describe the image.")[:200], return_tensors="pt")
        device = next(model.parameters()).device
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.no_grad():
            output = model.generate(**inputs, max_new_tokens=24)
        return processor.decode(output[0], skip_special_tokens=True).strip()
    except Exception:
        return ""


def _device_image(data_uri: str):
    from PIL import Image
    import io
    return Image.open(io.BytesIO(base64.b64decode(data_uri.split(",", 1)[-1]))).convert("RGB")


def _load_device(device_index: int):
    if device_index in _DEVICE_CACHE:
        return _DEVICE_CACHE[device_index]
    path = _model_dir()
    if path is None:
        return None, None
    try:
        import torch
        from transformers import BlipForConditionalGeneration, BlipProcessor
        processor = BlipProcessor.from_pretrained(str(path), local_files_only=True)
        model = BlipForConditionalGeneration.from_pretrained(str(path), local_files_only=True)
        model = model.to(f"cuda:{device_index}").eval()
        _DEVICE_CACHE[device_index] = (model, processor)
        return model, processor
    except Exception:
        return None, None


def caption_many(data_uris: list[str], max_devices: int = 4) -> list[str]:
    """Shard slide images across available CUDA devices, then batch each shard."""
    if not data_uris:
        return []
    try:
        import torch
        count = min(max(1, int(torch.cuda.device_count())), max_devices, len(data_uris))
    except Exception:
        count = 1
    if count <= 1:
        model, processor = _load()
        if model is None:
            return [caption(data_uri=uri) for uri in data_uris]
        import torch
        results = []
        device = next(model.parameters()).device
        for offset in range(0, len(data_uris), 6):
            batch = data_uris[offset:offset + 6]
            try:
                inputs = processor(images=[_device_image(uri) for uri in batch], return_tensors="pt")
                inputs = {key: value.to(device) for key, value in inputs.items()}
                with torch.no_grad():
                    output = model.generate(**inputs, max_new_tokens=40)
                results.extend(processor.decode(row, skip_special_tokens=True).strip() for row in output)
            except Exception:
                results.extend(caption(data_uri=uri) for uri in batch)
        return results
    groups = [(list(range(i, len(data_uris), count)), data_uris[i::count]) for i in range(count)]

    def work(index: int, indices: list[int], group: list[str]) -> dict[int, str]:
        import torch
        model, processor = _load_device(index)
        if model is None:
            return {}
        device = next(model.parameters()).device
        result = {}
        for offset in range(0, len(group), 4):
            batch = group[offset:offset + 4]
            batch_indices = indices[offset:offset + len(batch)]
            try:
                inputs = processor(images=[_device_image(uri) for uri in batch], return_tensors="pt")
                inputs = {key: value.to(device) for key, value in inputs.items()}
                with torch.no_grad():
                    output = model.generate(**inputs, max_new_tokens=40)
                for global_index, row in zip(batch_indices, output):
                    result[global_index] = processor.decode(row, skip_special_tokens=True).strip()
            except Exception:
                for global_index, uri in zip(batch_indices, batch):
                    result[global_index] = caption(data_uri=uri)
        return result

    import concurrent.futures
    merged = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(work, i, indices, group) for i, (indices, group) in enumerate(groups)]
        for future in futures:
            merged.update(future.result())
    return [merged.get(i, "") for i in range(len(data_uris))]


def answer_many(question: str, data_uris: list[str], max_devices: int = 4) -> list[str]:
    """Shard slide questions across GPUs; process groups sequentially for stability."""
    if not data_uris:
        return []
    try:
        import torch
        count = min(max(1, int(torch.cuda.device_count())), max_devices, len(data_uris))
    except Exception:
        count = 1
    if count <= 1:
        model, processor = _load_vqa()
        if model is None:
            return [answer(question, data_uri=uri) for uri in data_uris]
        import torch
        results = []
        device = next(model.parameters()).device
        for offset in range(0, len(data_uris), 4):
            batch = data_uris[offset:offset + 4]
            try:
                inputs = processor(images=[_device_image(uri) for uri in batch],
                                   text=[question] * len(batch), return_tensors="pt")
                inputs = {key: value.to(device) for key, value in inputs.items()}
                with torch.no_grad():
                    output = model.generate(**inputs, max_new_tokens=24)
                results.extend(processor.decode(row, skip_special_tokens=True).strip() for row in output)
            except Exception:
                results.extend(answer(question, data_uri=uri) for uri in batch)
        return results
    groups = [(list(range(i, len(data_uris), count)), data_uris[i::count]) for i in range(count)]
    merged = {}
    for index, (indices, group) in enumerate(groups):
        import torch
        model, processor = _load_device_for_vqa(index)
        if model is None:
            continue
        device = next(model.parameters()).device
        for offset in range(0, len(group), 2):
            batch = group[offset:offset + 2]
            batch_indices = indices[offset:offset + len(batch)]
            try:
                inputs = processor(images=[_device_image(uri) for uri in batch],
                                   text=[question] * len(batch), return_tensors="pt")
                inputs = {key: value.to(device) for key, value in inputs.items()}
                with torch.no_grad():
                    output = model.generate(**inputs, max_new_tokens=24)
                for global_index, row in zip(batch_indices, output):
                    merged[global_index] = processor.decode(row, skip_special_tokens=True).strip()
            except Exception:
                for global_index, uri in zip(batch_indices, batch):
                    merged[global_index] = answer(question, data_uri=uri)
    return [merged.get(i, "") for i in range(len(data_uris))]


def _load_device_for_vqa(device_index: int):
    if device_index in _VQA_DEVICE_CACHE:
        return _VQA_DEVICE_CACHE[device_index]
    candidates = []
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        candidates.append(Path(ai_home) / "models" / "vision" / "blip-vqa-base")
    candidates.append(Path(__file__).resolve().parent.parent / "ai" / "models" / "vision" / "blip-vqa-base")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "_ai_install" / "TwilightHarnessPortable" / "ai" / "models" / "vision" / "blip-vqa-base")
    path = next((item for item in candidates if (item / "config.json").is_file()), None)
    if path is None:
        return None, None
    try:
        import torch
        from transformers import BlipForQuestionAnswering, BlipProcessor
        processor = BlipProcessor.from_pretrained(str(path), local_files_only=True)
        model = BlipForQuestionAnswering.from_pretrained(str(path), local_files_only=True)
        model = model.to(f"cuda:{device_index}").eval()
        _VQA_DEVICE_CACHE[device_index] = (model, processor)
        return model, processor
    except Exception:
        return None, None


def caption(path: str | Path | None = None, data_uri: str = "") -> str:
    model, processor = _load()
    if model is None:
        return ""
    try:
        import torch
        from PIL import Image
        if path:
            image = Image.open(path).convert("RGB")
        elif data_uri:
            payload = data_uri.split(",", 1)[-1]
            image = Image.open(io.BytesIO(base64.b64decode(payload))).convert("RGB")
        else:
            return ""
        inputs = processor(images=image, return_tensors="pt")
        device = next(model.parameters()).device
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.no_grad():
            output = model.generate(**inputs, max_new_tokens=40)
        return processor.decode(output[0], skip_special_tokens=True).strip()
    except Exception:
        return ""


def release() -> None:
    """Drop cached vision models and return VRAM before another generator loads."""
    try:
        import gc
        _CACHE.update(model=None, processor=None)
        _DEVICE_CACHE.clear()
        _VQA_DEVICE_CACHE.clear()
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
