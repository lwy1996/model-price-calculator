from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional


CATALOG_PATH = Path(__file__).resolve().parent.parent / "assets" / "model-catalog.json"


def normalize_model_key(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace(".", "-")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


@lru_cache(maxsize=1)
def load_model_catalog() -> Dict[str, Any]:
    with open(CATALOG_PATH, "r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("model-catalog.json 顶层必须是对象")
    models = data.get("models")
    if not isinstance(models, list):
        raise ValueError("model-catalog.json 缺少 models 数组")
    return data


def catalog_models() -> List[Dict[str, Any]]:
    return load_model_catalog().get("models", [])


def alias_map() -> Dict[str, Dict[str, Any]]:
    aliases: Dict[str, Dict[str, Any]] = {}
    for model in catalog_models():
        canonical = model.get("canonical")
        keys = [canonical] + list(model.get("aliases") or [])
        for key in keys:
            normalized = normalize_model_key(key)
            if normalized:
                aliases[normalized] = model
    return aliases


def resolve_model(value: Any) -> Optional[Dict[str, Any]]:
    normalized = normalize_model_key(value)
    if not normalized:
        return None
    return alias_map().get(normalized)


def canonical_model_name(value: Any) -> str:
    model = resolve_model(value)
    if model:
        return str(model.get("canonical") or value)
    return "" if value is None else str(value).strip()


def model_defaults(value: Any) -> Dict[str, Any]:
    model = resolve_model(value)
    if not model:
        return {}
    defaults = model.get("defaults") or {}
    if not isinstance(defaults, dict):
        return {}
    result = dict(defaults)
    result["model_name"] = model.get("canonical")
    return result
