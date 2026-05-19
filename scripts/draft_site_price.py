#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from extract_model_price import extract_payload
from ingest_site_price import build_upsert_payload
from site_price_registry import load_registry, upsert_record


DRAFTS_PATH = Path(__file__).resolve().parent.parent / "assets" / "site-price-drafts.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


def load_drafts() -> Dict[str, Any]:
    if not DRAFTS_PATH.exists():
        return {"version": 1, "drafts": []}
    return load_json_file(str(DRAFTS_PATH))


def save_drafts(data: Dict[str, Any]) -> None:
    DRAFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DRAFTS_PATH, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def find_draft(data: Dict[str, Any], draft_id: str) -> Optional[Dict[str, Any]]:
    for draft in data.get("drafts", []):
        if draft.get("draft_id") == draft_id:
            return draft
    return None


def resolve_draft_id(payload: Dict[str, Any]) -> str:
    draft_id = normalize_text(payload.get("draft_id"))
    if draft_id:
        return draft_id

    station = payload.get("station") or {}
    for key in ("alias", "name", "station_name", "website", "api_base_url", "api_url"):
        value = normalize_text(station.get(key))
        if value:
            return value.lower()
    return "default"


def merge_non_empty(target: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in incoming.items():
        if isinstance(value, dict):
            target[key] = merge_non_empty(target.get(key) or {}, value)
        elif isinstance(value, list):
            if value:
                target[key] = value
        elif value not in (None, ""):
            target[key] = value
    return target


def build_patch(payload: Dict[str, Any]) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}

    station = payload.get("station") or {}
    if station:
        patch["station"] = station

    pricing = payload.get("pricing") or {}
    if pricing:
        patch["pricing"] = pricing

    raw_text = normalize_text(payload.get("raw_text"))
    if raw_text:
        patch["raw_text"] = raw_text
        extracted = extract_payload(raw_text)
        explicit_model = normalize_text(payload.get("model_name")) or normalize_text((payload.get("pricing") or {}).get("model_name"))
        raw_has_model_keyword = any(token in raw_text.lower() for token in ["gpt", "模型", "model"])
        if not explicit_model and not raw_has_model_keyword:
            extracted.pop("model_name", None)
        patch["pricing"] = merge_non_empty(patch.get("pricing") or {}, extracted)

    for key in ("model_name", "group", "multiplier", "recharge_ratio", "sale_price", "notes"):
        if payload.get(key) not in (None, ""):
            patch[key] = payload[key]

    if patch.get("model_name"):
        patch["pricing"] = patch.get("pricing") or {}
        patch["pricing"]["model_name"] = patch["model_name"]
    if patch.get("group"):
        patch["pricing"] = patch.get("pricing") or {}
        patch["pricing"]["group"] = patch["group"]
    if patch.get("multiplier") is not None:
        patch["pricing"] = patch.get("pricing") or {}
        patch["pricing"]["multiplier"] = patch["multiplier"]
    if patch.get("recharge_ratio"):
        patch["pricing"] = patch.get("pricing") or {}
        patch["pricing"]["recharge_ratio"] = patch["recharge_ratio"]
    if patch.get("sale_price"):
        patch["pricing"] = patch.get("pricing") or {}
        patch["pricing"]["sale_price"] = patch["sale_price"]

    return patch


def required_state(draft: Dict[str, Any]) -> Dict[str, Any]:
    station = draft.get("station") or {}
    pricing = draft.get("pricing") or {}

    has_station_identity = any(
        normalize_text(station.get(key))
        for key in ("alias", "name", "station_name", "website", "api_base_url", "api_url")
    )
    has_model = bool(normalize_text(pricing.get("model_name")))
    has_input = bool(normalize_text(pricing.get("input_price")) or normalize_text(pricing.get("输入价格")))
    has_output = bool(
        normalize_text(pricing.get("output_price"))
        or normalize_text(pricing.get("输出价格"))
        or normalize_text(pricing.get("补全价格"))
    )

    missing = []
    next_questions = []
    if not has_station_identity:
        missing.append("station_identity")
        next_questions.append("先给我一个能识别这个站的信息：站点别名、官网地址、或 API 地址，三选一即可。")
    if not has_model:
        missing.append("model_name")
        next_questions.append("这个站你要记录哪个模型？如果有分组也可以一起告诉我。")
    if not has_input:
        missing.append("input_price")
        next_questions.append("还缺输入价格，你直接说数值也行，比如 2.4 或 $2.4 / 1M。")
    if not has_output:
        missing.append("output_price")
        next_questions.append("还缺输出价格，你直接说数值也行，比如 14.5 或 $14.5 / 1M。")

    return {
        "is_ready": not missing,
        "missing_fields": missing,
        "next_questions": next_questions[:2],
    }


def summarize_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "draft_id": draft.get("draft_id"),
        "station": draft.get("station", {}),
        "pricing": draft.get("pricing", {}),
        "notes": draft.get("notes", ""),
        "updated_at": draft.get("updated_at"),
        "state": required_state(draft),
    }


def merge_draft(data: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    draft_id = resolve_draft_id(payload)
    draft = find_draft(data, draft_id)
    timestamp = now_iso()

    if draft is None:
        draft = {
            "draft_id": draft_id,
            "station": {},
            "pricing": {},
            "notes": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        data.setdefault("drafts", []).append(draft)

    patch = build_patch(payload)
    merge_non_empty(draft, patch)
    draft["draft_id"] = draft_id
    draft["updated_at"] = timestamp
    save_drafts(data)
    return summarize_draft(draft)


def show_draft(data: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    draft_id = resolve_draft_id(payload)
    draft = find_draft(data, draft_id)
    if draft is None:
        return {"draft_id": draft_id, "exists": False, "message": "当前没有这个草稿。"}
    result = summarize_draft(draft)
    result["exists"] = True
    return result


def clear_draft(data: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    draft_id = resolve_draft_id(payload)
    before = len(data.get("drafts", []))
    data["drafts"] = [draft for draft in data.get("drafts", []) if draft.get("draft_id") != draft_id]
    save_drafts(data)
    return {"draft_id": draft_id, "removed": len(data.get("drafts", [])) != before}


def commit_draft(data: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    draft_id = resolve_draft_id(payload)
    draft = find_draft(data, draft_id)
    if draft is None:
        raise ValueError("找不到可提交的草稿")

    state = required_state(draft)
    if not state["is_ready"]:
        return {
            "draft_id": draft_id,
            "committed": False,
            "state": state,
            "message": "草稿信息还不完整，先补齐必要字段再提交。",
        }

    upsert_payload = build_upsert_payload(draft)
    registry = load_registry()
    upsert_result = upsert_record(registry, upsert_payload)

    data["drafts"] = [item for item in data.get("drafts", []) if item.get("draft_id") != draft_id]
    save_drafts(data)
    return {
        "draft_id": draft_id,
        "committed": True,
        "upsert": upsert_result,
        "station_snapshot": upsert_result.get("station_snapshot"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage multi-turn site price drafts.")
    parser.add_argument("command", choices=["merge", "show", "commit", "clear"], help="Draft action")
    parser.add_argument("--json-file", required=True, help="Path to JSON payload file")
    args = parser.parse_args()

    payload = load_json_file(args.json_file)
    data = load_drafts()

    if args.command == "merge":
        result = merge_draft(data, payload)
    elif args.command == "show":
        result = show_draft(data, payload)
    elif args.command == "commit":
        result = commit_draft(data, payload)
    else:
        result = clear_draft(data, payload)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
