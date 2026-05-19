#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from calc_model_price import apply_official_model_defaults, compute, format_decimal, to_decimal


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "assets" / "site-price-registry.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


def load_registry() -> Dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"version": 1, "stations": [], "price_records": []}
    return load_json_file(str(REGISTRY_PATH))


def save_registry(registry: Dict[str, Any]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as file:
        json.dump(registry, file, ensure_ascii=False, indent=2)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = normalize_text(value).lower()
    return text in {"1", "true", "yes", "y", "是", "测试", "test"}


def normalize_key(value: Any) -> str:
    text = normalize_text(value).lower()
    text = re.sub(r"^https?://", "", text)
    text = text.rstrip("/")
    text = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE)
    text = text.replace("_", "-")
    return text.strip("-")


def identifier_variants(value: Any) -> List[str]:
    text = normalize_text(value)
    if not text:
        return []

    variants: List[str] = []
    normalized = normalize_key(text)
    if normalized:
        variants.append(normalized)

    lowered = text.lower().rstrip("/")
    if lowered and lowered not in variants:
        variants.append(lowered)

    trimmed_url = re.sub(r"^https?://", "", lowered)
    if trimmed_url and trimmed_url not in variants:
        variants.append(trimmed_url)

    return variants


def collect_station_match_keys(values: List[Any]) -> set[str]:
    keys: set[str] = set()
    for value in values:
        keys.update(identifier_variants(value))
    return keys


def ensure_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [normalize_text(item) for item in value if normalize_text(item)]
    text = normalize_text(value)
    return [text] if text else []


def unique_strings(values: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        normalized = normalize_text(value)
        if not normalized:
            continue
        marker = normalized.lower()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(normalized)
    return result


def resolve_station_recharge_ratio(station: Dict[str, Any], record: Optional[Dict[str, Any]] = None) -> str:
    station_ratio = normalize_text(station.get("recharge_ratio"))
    if station_ratio:
        return station_ratio
    if record is not None:
        record_ratio = normalize_text(record.get("recharge_ratio"))
        if record_ratio:
            return record_ratio
    return "1:1"


def resolve_station_summary_recharge_ratio(station: Dict[str, Any], summary: Dict[str, Any]) -> str:
    station_ratio = normalize_text(station.get("recharge_ratio"))
    if station_ratio:
        return station_ratio
    for record in summary.get("records", []):
        record_ratio = normalize_text(record.get("recharge_ratio"))
        if record_ratio:
            return record_ratio
    return "1:1"


def normalize_group_multiplier_map(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}

    normalized: Dict[str, float] = {}
    for key, raw in value.items():
        group = normalize_text(key).lower()
        if not group:
            continue
        numeric = to_decimal(raw)
        if numeric is None:
            continue
        normalized[group] = float(numeric)
    return normalized


def build_station_identifiers(station: Dict[str, Any], include_api: bool = True) -> List[str]:
    identifiers = []
    identifiers.extend(ensure_list(station.get("aliases")))
    identifiers.extend(
        ensure_list(
            station.get("alias")
            or station.get("site_alias")
            or station.get("name")
            or station.get("station_name")
        )
    )
    identifiers.extend(ensure_list(station.get("website")))
    if include_api:
        identifiers.extend(ensure_list(station.get("api_base_url")))
        identifiers.extend(ensure_list(station.get("api_url")))
    return unique_strings(identifiers)


def is_test_station(station: Dict[str, Any]) -> bool:
    return normalize_bool(station.get("is_test_data"))


def station_match_score(station: Dict[str, Any], payload: Dict[str, Any]) -> int:
    station_aliases = collect_station_match_keys(
        list(station.get("aliases", []))
        + [
            station.get("name"),
            station.get("website"),
            station.get("api_base_url"),
        ]
    )

    payload_identifiers = build_station_identifiers(payload)
    score = 0
    for identifier in payload_identifiers:
        variants = identifier_variants(identifier)
        if variants and any(variant in station_aliases for variant in variants):
            score += 1
    return score


def find_station(registry: Dict[str, Any], station_payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], int]:
    best_station = None
    best_score = 0
    for station in registry.get("stations", []):
        score = station_match_score(station, station_payload)
        if score > best_score:
            best_station = station
            best_score = score
    return best_station, best_score


def derive_station_id(station_payload: Dict[str, Any]) -> str:
    candidates = [
        station_payload.get("alias"),
        station_payload.get("site_alias"),
        station_payload.get("name"),
        station_payload.get("station_name"),
        station_payload.get("website"),
        station_payload.get("api_base_url"),
        station_payload.get("api_url"),
    ]
    for candidate in candidates:
        key = normalize_key(candidate)
        if key:
            return key
    for candidate in candidates:
        raw = normalize_text(candidate).lower()
        if raw:
            digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
            return f"station-{digest}"
    return f"station-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def upsert_station(registry: Dict[str, Any], station_payload: Dict[str, Any]) -> Dict[str, Any]:
    station, score = find_station(registry, station_payload)
    timestamp = now_iso()
    if station is None or score == 0:
        station = {
            "station_id": derive_station_id(station_payload),
            "name": normalize_text(
                station_payload.get("name")
                or station_payload.get("station_name")
                or station_payload.get("alias")
                or station_payload.get("site_alias")
            ),
            "aliases": unique_strings(build_station_identifiers(station_payload, include_api=False)),
            "website": normalize_text(station_payload.get("website")),
            "recharge_ratio": normalize_text(station_payload.get("recharge_ratio") or station_payload.get("充值比")) or "1:1",
            "group_multipliers": normalize_group_multiplier_map(station_payload.get("group_multipliers")),
            "is_test_data": normalize_bool(station_payload.get("is_test_data")),
            "notes": normalize_text(station_payload.get("notes")),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        registry.setdefault("stations", []).append(station)
        return station

    station["name"] = normalize_text(station_payload.get("name") or station.get("name"))
    station["website"] = normalize_text(station_payload.get("website") or station.get("website"))
    station["recharge_ratio"] = (
        normalize_text(station_payload.get("recharge_ratio") or station_payload.get("充值比"))
        or normalize_text(station.get("recharge_ratio"))
        or "1:1"
    )
    incoming_group_multipliers = normalize_group_multiplier_map(station_payload.get("group_multipliers"))
    if incoming_group_multipliers:
        merged_group_multipliers = dict(station.get("group_multipliers") or {})
        merged_group_multipliers.update(incoming_group_multipliers)
        station["group_multipliers"] = merged_group_multipliers
    if "is_test_data" in station_payload:
        station["is_test_data"] = normalize_bool(station_payload.get("is_test_data"))
    station["notes"] = normalize_text(station_payload.get("notes") or station.get("notes"))
    station["aliases"] = unique_strings(station.get("aliases", []) + build_station_identifiers(station_payload))
    station["updated_at"] = timestamp
    return station


def update_station_fields(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    station_payload = deepcopy(payload.get("station") or payload)
    station, score = find_station(registry, station_payload)
    if station is None or score == 0:
        raise ValueError("未找到可更新的中转站，请先提供已收录的别名、官网或 API 地址")

    changed_fields = []
    timestamp = now_iso()

    if "name" in station_payload or "station_name" in station_payload:
        value = normalize_text(station_payload.get("name") or station_payload.get("station_name"))
        if value and value != station.get("name"):
            station["name"] = value
            changed_fields.append("name")

    if "website" in station_payload:
        value = normalize_text(station_payload.get("website"))
        if value and value != station.get("website"):
            station["website"] = value
            changed_fields.append("website")

    if "recharge_ratio" in station_payload or "充值比" in station_payload:
        value = normalize_text(station_payload.get("recharge_ratio") or station_payload.get("充值比")) or "1:1"
        if value != resolve_station_recharge_ratio(station):
            station["recharge_ratio"] = value
            changed_fields.append("recharge_ratio")

    if "group_multipliers" in station_payload:
        value = normalize_group_multiplier_map(station_payload.get("group_multipliers"))
        if value and value != (station.get("group_multipliers") or {}):
            station["group_multipliers"] = value
            changed_fields.append("group_multipliers")

    if "is_test_data" in station_payload:
        value = normalize_bool(station_payload.get("is_test_data"))
        if value != normalize_bool(station.get("is_test_data")):
            station["is_test_data"] = value
            changed_fields.append("is_test_data")

    if "notes" in station_payload:
        value = normalize_text(station_payload.get("notes"))
        if value != station.get("notes", ""):
            station["notes"] = value
            changed_fields.append("notes")

    new_aliases = unique_strings(station.get("aliases", []) + build_station_identifiers(station_payload, include_api=False))
    if new_aliases != station.get("aliases", []):
        station["aliases"] = new_aliases
        changed_fields.append("aliases")

    station["updated_at"] = timestamp
    if "recharge_ratio" in changed_fields:
        recompute_station_records(registry, station, timestamp)
    save_registry(registry)
    return {
        "station": station,
        "changed_fields": unique_strings(changed_fields),
        "station_snapshot": build_station_snapshot(registry, station),
    }


def normalized_group(value: Any) -> str:
    group = normalize_text(value)
    return group if group else "default"


def find_record_index(registry: Dict[str, Any], station_id: str, model_name: str, group: str) -> Optional[int]:
    for index, record in enumerate(registry.get("price_records", [])):
        if (
            record.get("station_id") == station_id
            and normalize_key(record.get("model_name")) == normalize_key(model_name)
            and normalize_key(record.get("group")) == normalize_key(group)
        ):
            return index
    return None


def find_station_model_records(registry: Dict[str, Any], station_id: str, model_name: str) -> List[Dict[str, Any]]:
    matched = []
    for record in registry.get("price_records", []):
        if (
            record.get("station_id") == station_id
            and normalize_key(record.get("model_name")) == normalize_key(model_name)
        ):
            matched.append(record)
    matched.sort(
        key=lambda item: normalize_text(item.get("updated_at") or item.get("created_at")),
        reverse=True,
    )
    return matched


def infer_pricing_defaults(
    registry: Dict[str, Any],
    station_id: str,
    model_name: str,
    explicit_group: str,
) -> Dict[str, Any]:
    matched_records = find_station_model_records(registry, station_id, model_name)
    if not matched_records:
        return {}

    if explicit_group:
        for record in matched_records:
            if normalize_key(record.get("group")) == normalize_key(explicit_group):
                return record

    return matched_records[0]


def build_record_pricing_payload(station: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model_name": record.get("model_name"),
        "group": record.get("group"),
        "input_price": record.get("input_price"),
        "output_price": record.get("output_price"),
        "cache_price": record.get("cache_price"),
        "cache_read_price": record.get("cache_read_price"),
        "cache_write_price": record.get("cache_write_price"),
        "group_note": record.get("group_note"),
        "multiplier": record.get("multiplier"),
        "recharge_ratio": resolve_station_recharge_ratio(station, record),
        "sale_price": record.get("sale_price"),
    }


def recompute_station_records(registry: Dict[str, Any], station: Dict[str, Any], timestamp: Optional[str] = None) -> None:
    station_id = station.get("station_id")
    if not station_id:
        return
    updated_at = timestamp or now_iso()
    for index, record in enumerate(registry.get("price_records", [])):
        if record.get("station_id") != station_id:
            continue
        refreshed_record = deepcopy(record)
        refreshed_record["recharge_ratio"] = resolve_station_recharge_ratio(station, record)
        refreshed_record["computed"] = compute(build_record_pricing_payload(station, refreshed_record))
        refreshed_record["updated_at"] = updated_at
        registry["price_records"][index] = refreshed_record


def build_rank_item(registry: Dict[str, Any], record: Dict[str, Any], metric: str) -> Optional[Dict[str, Any]]:
    station = next((item for item in registry.get("stations", []) if item.get("station_id") == record.get("station_id")), {})
    computed = compute(build_record_pricing_payload(station, record))
    dimensions = computed.get("dimensions", {}) if isinstance(computed.get("dimensions", {}), dict) else {}
    input_dimension = dimensions.get("input") or {}
    output_dimension = dimensions.get("output") or {}
    cache_read_dimension = dimensions.get("cache_read") or {}
    cache_write_dimension = dimensions.get("cache_write") or {}
    metric_map = {
        "input_rmb_per_m": input_dimension.get("rmb_per_m"),
        "output_rmb_per_m": output_dimension.get("rmb_per_m"),
        "cache_read_rmb_per_m": cache_read_dimension.get("rmb_per_m"),
        "cache_write_rmb_per_m": cache_write_dimension.get("rmb_per_m"),
        "summary_rmb_per_m": computed.get("summary", {}).get("rmb_per_m"),
        "output_input_ratio": computed.get("comparisons", {}).get("output_input_ratio"),
        "cache_read_discount_vs_input": computed.get("comparisons", {}).get("cache_read_discount_vs_input"),
    }
    raw_value = metric_map.get(metric)
    numeric = to_decimal(raw_value)
    if numeric is None:
        return None

    return {
        "station_id": record.get("station_id"),
        "station_name": station.get("name") or record.get("station_id"),
        "aliases": station.get("aliases", []),
        "website": station.get("website"),
        "model_name": record.get("model_name"),
        "group": record.get("group"),
        "metric": metric,
        "value": format_decimal(numeric),
        "_sort": numeric,
        "copy_text": computed.get("copy_text"),
        "updated_at": record.get("updated_at"),
    }


def rank_records(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    model_name = normalize_text(query.get("model_name"))
    group = normalize_text(query.get("group"))
    metric = normalize_text(query.get("sort_by") or query.get("metric") or "summary_rmb_per_m")
    direction = normalize_text(query.get("direction") or "asc").lower()

    matched = []
    for record in registry.get("price_records", []):
        if model_name and normalize_key(record.get("model_name")) != normalize_key(model_name):
            continue
        if group and normalize_key(record.get("group")) != normalize_key(group):
            continue
        item = build_rank_item(registry, record, metric)
        if item is None:
            continue
        matched.append(item)

    reverse = direction == "desc"
    matched.sort(key=lambda item: item["_sort"], reverse=reverse)
    for item in matched:
        item.pop("_sort", None)

    return {
        "filters": {
            "model_name": model_name or None,
            "group": group or None,
            "metric": metric,
            "direction": direction,
        },
        "count": len(matched),
        "items": matched,
    }


def normalize_model_alias(value: Any) -> str:
    text = normalize_text(value).lower()
    if not text:
        return ""

    compact = re.sub(r"[\s_\-]+", "", text)
    compact = compact.replace("gpt", "")
    compact = compact.replace("模型", "")
    compact = compact.strip()

    if "mini" in compact and re.search(r"5[.\-]?4|54", compact):
        return "gpt-5.4-mini"
    if re.search(r"5[.\-]?5|55", compact):
        return "gpt-5.5"
    if re.search(r"5[.\-]?4|54", compact):
        return "gpt-5.4"
    return normalize_text(value)


def extract_quick_limit(text: str, default: int = 10) -> int:
    patterns = [
        r"(?:top|Top|TOP)\s*(\d+)",
        r"前\s*(\d+)",
        r"最便宜\D{0,8}(\d+)\s*(?:个|家|条|站点)?",
        r"(\d+)\s*(?:个|家|条)\s*(?:站点)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return default


def extract_quick_model(text: str) -> str:
    lowered = text.lower()
    candidates = [
        r"gpt[\s\-_]*5[.\-]?4[\s\-_]*mini",
        r"5[.\-]?4[\s\-_]*mini",
        r"gpt[\s\-_]*5[.\-]?5",
        r"gpt[\s\-_]*5[.\-]?4",
        r"\b5[.\-]?5\b",
        r"\b5[.\-]?4\b",
        r"\b55\b",
        r"\b54\b",
        r"mini",
    ]
    for pattern in candidates:
        match = re.search(pattern, lowered)
        if match:
            return normalize_model_alias(match.group(0))
    return ""


def extract_quick_metric(text: str) -> str:
    if re.search(r"缓存写|缓存创建|cache\s*write", text, re.I):
        return "cache_write_rmb_per_m"
    if re.search(r"缓存|cache", text, re.I):
        return "cache_read_rmb_per_m"
    if re.search(r"输出|补全|output|completion", text, re.I):
        return "output_rmb_per_m"
    if re.search(r"输入|input|prompt", text, re.I):
        return "input_rmb_per_m"
    return "summary_rmb_per_m"


def extract_quick_group(text: str) -> str:
    explicit = re.search(r"(?:分组|group)\s*[:：]?\s*([A-Za-z0-9_\-\u4e00-\u9fff]+)", text, re.I)
    if explicit:
        return explicit.group(1)

    known_groups = [
        "default",
        "free",
        "plus",
        "pro",
        "vip",
        "svip",
        "codex",
        "code-plus",
        "code-pro",
        "限时特价",
        "默认",
    ]
    lowered = text.lower()
    for group in known_groups:
        if group.lower() in lowered:
            return group
    return ""


def is_quick_rank_query(text: str, payload: Dict[str, Any]) -> bool:
    if payload.get("mode") == "rank":
        return True
    if payload.get("model_name") or payload.get("模型名称"):
        return True
    has_rank_word = bool(re.search(r"最便宜|排行|排名|top|前\s*\d+|便宜|最低", text, re.I))
    return has_rank_word and bool(extract_quick_model(text))


def build_quick_rank_query(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = normalize_text(payload.get("query") or payload.get("q") or payload.get("keyword"))
    model_name = normalize_model_alias(payload.get("model_name") or payload.get("模型名称")) or extract_quick_model(text)
    group = normalize_text(payload.get("group") or payload.get("分组")) or extract_quick_group(text)
    metric = normalize_text(payload.get("sort_by") or payload.get("metric")) or extract_quick_metric(text)
    direction = normalize_text(payload.get("direction") or "asc").lower()
    limit = payload.get("limit") or extract_quick_limit(text, default=10)
    return {
        "model_name": model_name,
        "group": group,
        "sort_by": metric,
        "direction": direction,
        "limit": limit,
    }


def clean_quick_search_text(text: str) -> str:
    cleaned = normalize_text(text)
    cleaned = re.sub(r"^(?:查|搜索|检索|找|查看|列出|帮我查|帮我找)\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(?:站点|中转站|官网|备注|分组备注|关键词)\s*[:：]?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*(?:的)?(?:站点|中转站|列表)$", "", cleaned, flags=re.I)
    return cleaned.strip()


def station_search_fields(station: Dict[str, Any], records: List[Dict[str, Any]]) -> List[Tuple[str, str, int]]:
    fields: List[Tuple[str, str, int]] = [
        ("站点ID", normalize_text(station.get("station_id")), 5),
        ("站点名称", normalize_text(station.get("name")), 8),
        ("官网", normalize_text(station.get("website")), 6),
        ("API", normalize_text(station.get("api_base_url") or station.get("api_url")), 5),
        ("充值比", normalize_text(station.get("recharge_ratio")), 2),
        ("备注", normalize_text(station.get("notes")), 4),
    ]
    for alias in station.get("aliases") or []:
        fields.append(("别名", normalize_text(alias), 7))
    for record in records:
        fields.extend(
            [
                ("模型", normalize_text(record.get("model_name")), 4),
                ("分组", normalize_text(record.get("group")), 4),
                ("分组备注", normalize_text(record.get("group_note")), 5),
            ]
        )
        for tag in ensure_list(record.get("tags")):
            fields.append(("标签", tag, 3))
    return fields


def search_stations_full_text(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    raw_keyword = normalize_text(query.get("keyword") or query.get("query") or query.get("q"))
    keyword = clean_quick_search_text(raw_keyword)
    limit = int(query.get("limit") or 20)
    if not keyword:
        return {
            "count": 0,
            "items": [],
            "station_ids": [],
            "keyword": keyword,
        }

    normalized_keyword = normalize_key(keyword)
    lowered_keyword = keyword.lower()
    records_by_station: Dict[str, List[Dict[str, Any]]] = {}
    for record in registry.get("price_records", []):
        records_by_station.setdefault(normalize_text(record.get("station_id")), []).append(record)

    items = []
    for station in registry.get("stations", []):
        station_id = normalize_text(station.get("station_id"))
        records = records_by_station.get(station_id, [])
        score = 0
        reasons = []
        seen_reasons = set()
        for label, value, weight in station_search_fields(station, records):
            if not value:
                continue
            normalized_value = normalize_key(value)
            lowered_value = value.lower()
            matched = lowered_keyword in lowered_value
            if not matched and normalized_keyword:
                matched = normalized_keyword in normalized_value
            if not matched:
                continue
            score += weight
            reason = f"{label} 命中「{keyword}」"
            if reason not in seen_reasons:
                seen_reasons.add(reason)
                reasons.append(reason)
        if score <= 0:
            continue
        items.append(
            {
                "station_id": station_id,
                "station_name": station.get("name") or station_id,
                "website": station.get("website"),
                "recharge_ratio": resolve_station_summary_recharge_ratio(station, {"records": records}),
                "notes": station.get("notes"),
                "score": score,
                "reasons": reasons[:3],
                "record_count": len(records),
            }
        )

    items.sort(key=lambda item: (-item["score"], normalize_text(item.get("station_name")).lower()))
    if limit > 0:
        items = items[:limit]
    return {
        "count": len(items),
        "items": items,
        "station_ids": [item["station_id"] for item in items],
        "keyword": keyword,
    }


def build_compact_search_markdown(registry: Dict[str, Any], search_result: Dict[str, Any]) -> str:
    stations_by_id = {
        normalize_text(station.get("station_id")): station
        for station in registry.get("stations", [])
    }
    lines = []
    for index, item in enumerate(search_result.get("items", []), start=1):
        station = stations_by_id.get(item.get("station_id"), {})
        summary = station_record_summary(registry, item.get("station_id"))
        best_record_text = "暂无价格记录"
        best_items = []
        for record in summary.get("records", []):
            rank_item = build_rank_item(registry, record, "summary_rmb_per_m")
            if rank_item:
                best_items.append(rank_item)
        if best_items:
            best_items.sort(key=lambda rank_item: to_decimal(rank_item.get("value")) or 0)
            best = best_items[0]
            best_record_text = f"{best.get('model_name')} / {best.get('group')} 综合价 {best.get('value')}/M"
        reasons = "；".join(item.get("reasons") or []) or "关键词命中"
        lines.append(f"{index}. {station.get('name') or item.get('station_id')}")
        lines.append(f"官网：{normalize_text(station.get('website')) or '未记录'}")
        lines.append(f"充值比：{resolve_station_summary_recharge_ratio(station, summary)}")
        lines.append(f"匹配：{reasons}")
        lines.append(f"最优摘要：{best_record_text}")
        lines.append("")
    return "\n".join(lines).strip() or "暂无站点"


def quick_query(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    text = normalize_text(payload.get("query") or payload.get("q") or payload.get("keyword"))
    view = normalize_text(payload.get("view") or payload.get("display") or "detail").lower()
    if is_quick_rank_query(text, payload):
        rank_query = build_quick_rank_query(payload)
        result = build_rank_station_markdown(registry, rank_query)
        result.update(
            {
                "mode": "rank",
                "query": text,
                "parsed": rank_query,
            }
        )
        return result

    search_payload = {
        "query": text,
        "keyword": payload.get("keyword") or text,
        "limit": payload.get("limit") or extract_quick_limit(text, default=20),
    }
    search_result = search_stations_full_text(registry, search_payload)
    if view in {"compact", "简洁", "摘要"}:
        text_output = build_compact_search_markdown(registry, search_result)
    else:
        text_output = build_station_markdown(registry, {"station_ids": search_result.get("station_ids", [])}).get("text", "")
    return {
        "mode": "search",
        "query": text,
        "keyword": search_result.get("keyword"),
        "count": search_result.get("count", 0),
        "items": search_result.get("items", []),
        "station_ids": search_result.get("station_ids", []),
        "text": text_output or "暂无站点",
    }


def search_registry(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    station, score = find_station(registry, query)
    if station is None or score == 0:
        return {
            "found": False,
            "match_score": 0,
            "station": None,
            "records": [],
        }

    records = [record for record in registry.get("price_records", []) if record.get("station_id") == station.get("station_id")]
    return {
        "found": True,
        "match_score": score,
        "station": station,
        "records": records,
    }


def build_station_snapshot(registry: Dict[str, Any], station: Dict[str, Any]) -> Dict[str, Any]:
    station_id = normalize_text(station.get("station_id"))
    keyword = station_id or normalize_text(station.get("name") or station.get("website"))
    return build_station_markdown(registry, {"keyword": keyword})


def upsert_record(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    station_payload = deepcopy(payload.get("station") or {})
    pricing_payload = deepcopy(payload.get("pricing") or {})

    if not station_payload:
        raise ValueError("缺少 station 对象")
    if not pricing_payload:
        raise ValueError("缺少 pricing 对象")

    station = upsert_station(registry, station_payload)
    station_ratio_override = (
        normalize_text(station_payload.get("recharge_ratio") or station_payload.get("充值比"))
        or normalize_text(payload.get("recharge_ratio") or payload.get("充值比"))
        or normalize_text(pricing_payload.get("recharge_ratio") or pricing_payload.get("充值比"))
    )
    if station_ratio_override:
        station["recharge_ratio"] = station_ratio_override
    pricing_payload.setdefault("model_name", pricing_payload.get("模型名称") or "gpt5.4")
    model_name = pricing_payload.get("model_name")
    explicit_group = normalize_text(pricing_payload.get("group") or pricing_payload.get("分组"))
    inherited_record = infer_pricing_defaults(registry, station["station_id"], model_name, explicit_group)
    if inherited_record:
        if not explicit_group and inherited_record.get("group"):
            pricing_payload["group"] = inherited_record.get("group")
        if pricing_payload.get("multiplier") in (None, "") and pricing_payload.get("倍率") in (None, ""):
            inherited_multiplier = inherited_record.get("multiplier")
            if inherited_multiplier not in (None, ""):
                pricing_payload["multiplier"] = inherited_multiplier

    group = normalized_group(pricing_payload.get("group") or pricing_payload.get("分组"))
    pricing_payload["group"] = group
    if pricing_payload.get("multiplier") in (None, "") and pricing_payload.get("倍率") in (None, ""):
        station_group_multipliers = station.get("group_multipliers") or {}
        station_multiplier = station_group_multipliers.get(group.lower())
        if station_multiplier not in (None, ""):
            pricing_payload["multiplier"] = station_multiplier

    pricing_payload["recharge_ratio"] = resolve_station_recharge_ratio(station, inherited_record)

    pricing_payload = apply_official_model_defaults(pricing_payload)

    computed = compute(pricing_payload)
    timestamp = now_iso()
    record = {
        "record_id": f"{station['station_id']}::{normalize_key(computed['model_name'])}::{normalize_key(group)}",
        "station_id": station["station_id"],
        "model_name": computed["model_name"],
        "group": group,
        "source": normalize_text(payload.get("source") or "manual"),
        "currency_hint": normalize_text(payload.get("currency_hint")),
        "input_price": pricing_payload.get("input") or pricing_payload.get("input_price") or pricing_payload.get("输入价格"),
        "output_price": (
            pricing_payload.get("output")
            or pricing_payload.get("output_price")
            or pricing_payload.get("输出价格")
            or pricing_payload.get("补全价格")
        ),
        "cache_price": pricing_payload.get("cache") or pricing_payload.get("cache_price") or pricing_payload.get("缓存价格"),
        "cache_read_price": (
            pricing_payload.get("cache_read")
            or pricing_payload.get("cache_read_price")
            or pricing_payload.get("缓存读取价格")
        ),
        "cache_write_price": (
            pricing_payload.get("cache_write")
            or pricing_payload.get("cache_write_price")
            or pricing_payload.get("缓存创建价格")
        ),
        "group_note": pricing_payload.get("group_note") or pricing_payload.get("分组备注"),
        "multiplier": pricing_payload.get("multiplier") or pricing_payload.get("倍率") or 1,
        "recharge_ratio": resolve_station_recharge_ratio(station),
        "sale_price": pricing_payload.get("sale_price") or pricing_payload.get("售价") or pricing_payload.get("站点售价"),
        "tags": unique_strings(ensure_list(payload.get("tags"))),
        "computed": computed,
        "updated_at": timestamp,
    }

    record_index = find_record_index(registry, station["station_id"], computed["model_name"], group)
    if record_index is None:
        record["created_at"] = timestamp
        registry.setdefault("price_records", []).append(record)
        action = "created"
    else:
        record["created_at"] = registry["price_records"][record_index].get("created_at", timestamp)
        registry["price_records"][record_index] = record
        action = "updated"

    station["updated_at"] = timestamp
    save_registry(registry)
    return {
        "action": action,
        "station": station,
        "record": record,
        "station_snapshot": build_station_snapshot(registry, station),
    }


def patch_record_fields(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    station_payload = deepcopy(payload.get("station") or {})
    station, score = find_station(registry, station_payload)
    if station is None or score == 0:
        raise ValueError("未找到可更新的中转站，请先提供已收录的别名、官网或 API 地址")

    model_name = normalize_text(payload.get("model_name") or payload.get("模型名称"))
    group = normalized_group(payload.get("group") or payload.get("分组"))
    if not model_name:
        raise ValueError("更新价格记录时缺少模型名称")

    record_index = find_record_index(registry, station["station_id"], model_name, group)
    if record_index is None:
        raise ValueError("未找到对应模型/分组记录，请先收录该模型价格")

    record = deepcopy(registry["price_records"][record_index])
    changed_fields = []

    field_map = {
        "input_price": ["input_price", "输入价格"],
        "output_price": ["output_price", "输出价格", "补全价格"],
        "cache_price": ["cache_price", "缓存价格"],
        "cache_read_price": ["cache_read_price", "缓存读取价格"],
        "cache_write_price": ["cache_write_price", "缓存创建价格"],
        "group_note": ["group_note", "分组备注"],
        "multiplier": ["multiplier", "倍率"],
        "sale_price": ["sale_price", "售价", "站点售价"],
    }

    pricing_payload: Dict[str, Any] = {
        "model_name": record.get("model_name"),
        "group": record.get("group"),
        "input_price": record.get("input_price"),
        "output_price": record.get("output_price"),
        "cache_price": record.get("cache_price"),
        "cache_read_price": record.get("cache_read_price"),
        "cache_write_price": record.get("cache_write_price"),
        "group_note": record.get("group_note"),
        "multiplier": record.get("multiplier"),
        "recharge_ratio": resolve_station_recharge_ratio(station, record),
        "sale_price": record.get("sale_price"),
    }

    station_ratio_changed = False
    if "recharge_ratio" in payload or "充值比" in payload:
        value = normalize_text(payload.get("recharge_ratio") or payload.get("充值比")) or "1:1"
        if value != resolve_station_recharge_ratio(station):
            station["recharge_ratio"] = value
            station_ratio_changed = True
        pricing_payload["recharge_ratio"] = resolve_station_recharge_ratio(station)

    for target_field, source_keys in field_map.items():
        for source_key in source_keys:
            if source_key in payload:
                value = payload[source_key]
                if pricing_payload.get(target_field) != value:
                    pricing_payload[target_field] = value
                    changed_fields.append(target_field)
                break

    recomputed = compute(pricing_payload)
    record.update(
        {
            "input_price": pricing_payload.get("input_price"),
            "output_price": pricing_payload.get("output_price"),
            "cache_price": pricing_payload.get("cache_price"),
            "cache_read_price": pricing_payload.get("cache_read_price"),
            "cache_write_price": pricing_payload.get("cache_write_price"),
            "group_note": pricing_payload.get("group_note"),
            "multiplier": pricing_payload.get("multiplier"),
            "recharge_ratio": resolve_station_recharge_ratio(station),
            "sale_price": pricing_payload.get("sale_price"),
            "computed": recomputed,
            "updated_at": now_iso(),
        }
    )
    registry["price_records"][record_index] = record
    if station_ratio_changed:
        station["updated_at"] = record["updated_at"]
        recompute_station_records(registry, station, record["updated_at"])
    save_registry(registry)

    return {
        "station": station,
        "record": record,
        "changed_fields": unique_strings(changed_fields),
        "station_snapshot": build_station_snapshot(registry, station),
    }


def list_registry(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    station_id = normalize_text(query.get("station_id"))
    model_name = normalize_text(query.get("model_name"))
    items = []
    for record in registry.get("price_records", []):
        if station_id and record.get("station_id") != station_id:
            continue
        if model_name and normalize_key(record.get("model_name")) != normalize_key(model_name):
            continue
        items.append(record)
    return {
        "count": len(items),
        "items": items,
    }


def iso_to_display(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return "未记录"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def station_record_summary(registry: Dict[str, Any], station_id: str) -> Dict[str, Any]:
    records = [record for record in registry.get("price_records", []) if record.get("station_id") == station_id]
    model_groups = [f"{record.get('model_name')} / {record.get('group')}" for record in records]
    return {
        "record_count": len(records),
        "model_groups": model_groups,
        "records": records,
    }


def station_completeness(station: Dict[str, Any]) -> str:
    score = 0
    if normalize_text(station.get("website")):
        score += 1
    if normalize_text(station.get("notes")):
        score += 1

    if score == 2:
        return "完整"
    if score == 1:
        return "较完整"
    return "待补充"


def build_original_price_text(record: Dict[str, Any]) -> str:
    parts = []
    if record.get("input_price"):
        parts.append(f"输入 {record.get('input_price')}")
    if record.get("output_price"):
        parts.append(f"输出 {record.get('output_price')}")
    if record.get("cache_read_price"):
        parts.append(f"缓存读 {record.get('cache_read_price')}")
    elif record.get("cache_price"):
        parts.append(f"缓存 {record.get('cache_price')}")
    if record.get("cache_write_price"):
        parts.append(f"缓存写 {record.get('cache_write_price')}")
    return " · ".join(parts) if parts else "未记录"


def trim_decimal_text(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return "-"
    if "." not in text:
        return text
    text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def format_rmb_per_m(value: Any) -> str:
    trimmed = trim_decimal_text(value)
    if trimmed == "-":
        return "-"
    return f"{trimmed}/M"


def build_computed_price_text(record: Dict[str, Any], computed: Optional[Dict[str, Any]] = None) -> str:
    computed = computed or record.get("computed", {})
    dimensions = computed.get("dimensions", {}) if isinstance(computed.get("dimensions"), dict) else {}
    parts = []
    input_dimension = dimensions.get("input") or {}
    output_dimension = dimensions.get("output") or {}
    cache_read_dimension = dimensions.get("cache_read") or {}
    cache_dimension = dimensions.get("cache") or {}
    cache_write_dimension = dimensions.get("cache_write") or {}
    if input_dimension.get("rmb_per_m") is not None:
        parts.append(f"输入 {format_rmb_per_m(input_dimension.get('rmb_per_m'))}")
    if output_dimension.get("rmb_per_m") is not None:
        parts.append(f"输出 {format_rmb_per_m(output_dimension.get('rmb_per_m'))}")
    if cache_read_dimension.get("rmb_per_m") is not None:
        parts.append(f"缓存读 {format_rmb_per_m(cache_read_dimension.get('rmb_per_m'))}")
    elif cache_dimension.get("rmb_per_m") is not None:
        parts.append(f"缓存 {format_rmb_per_m(cache_dimension.get('rmb_per_m'))}")
    if cache_write_dimension.get("rmb_per_m") is not None:
        parts.append(f"缓存写 {format_rmb_per_m(cache_write_dimension.get('rmb_per_m'))}")
    return " · ".join(parts) if parts else "未记录"


def append_station_markdown_block(
    lines: List[str],
    station: Dict[str, Any],
    summary: Dict[str, Any],
    index: int,
) -> None:
    website = normalize_text(station.get("website")) or "未记录"
    recharge_ratio = resolve_station_summary_recharge_ratio(station, summary)
    notes = normalize_text(station.get("notes")) or "无"
    marker = "（测试）" if is_test_station(station) else ""

    lines.append(f"{index}. {station.get('name') or station.get('station_id')}{marker}")
    lines.append(f"官网：{website}")
    lines.append(f"充值比：{recharge_ratio}")
    lines.append(f"备注：{notes}")
    if is_test_station(station):
        lines.append("标识：测试数据")
    lines.append(f"创建时间：{iso_to_display(station.get('created_at'))}")
    lines.append(f"最后更新：{iso_to_display(station.get('updated_at'))}")
    lines.append("")

    has_group_note = any(normalize_text(record.get("group_note")) for record in summary["records"])
    if has_group_note:
        lines.append("| 模型 | 分组 | 分组备注 | 倍率 | 折算价格 | 综合价 |")
        lines.append("|---|---|---|---:|---|---:|")
    else:
        lines.append("| 模型 | 分组 | 倍率 | 折算价格 | 综合价 |")
        lines.append("|---|---|---:|---|---:|")

    for record in summary["records"]:
        computed = compute(build_record_pricing_payload(station, record))
        multiplier = trim_decimal_text(computed.get("multiplier") or record.get("multiplier") or "1")
        group_note = normalize_text(record.get("group_note")) or "-"
        summary_cost = format_rmb_per_m(computed.get("summary", {}).get("rmb_per_m") or "-")
        if has_group_note:
            lines.append(
                f"| {record.get('model_name')} | {record.get('group')} | {group_note} | {multiplier} | "
                f"{build_computed_price_text(record, computed)} | {summary_cost} |"
            )
        else:
            lines.append(
                f"| {record.get('model_name')} | {record.get('group')} | {multiplier} | "
                f"{build_computed_price_text(record, computed)} | {summary_cost} |"
            )

    if not summary["records"]:
        if has_group_note:
            lines.append("| - | - | - | - | 暂无价格记录 | - |")
        else:
            lines.append("| - | - | - | 暂无价格记录 | - |")
    lines.append("")


def build_station_markdown(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    keyword = normalize_text(query.get("keyword"))
    station_ids = [
        normalize_text(station_id)
        for station_id in ensure_list(query.get("station_ids"))
        if normalize_text(station_id)
    ]
    stations = registry.get("stations", [])
    if station_ids:
        stations_by_id = {
            normalize_text(station.get("station_id")): station
            for station in stations
        }
        stations = [stations_by_id[station_id] for station_id in station_ids if station_id in stations_by_id]
    elif keyword:
        lowered = keyword.lower()
        filtered = []
        for station in stations:
            haystacks = [
                station.get("station_id"),
                station.get("name"),
                station.get("website"),
                station.get("api_base_url"),
                station.get("api_url"),
                station.get("notes"),
                *(station.get("aliases") or []),
            ]
            if any(lowered in normalize_text(item).lower() for item in haystacks):
                filtered.append(station)
        stations = filtered

    if not station_ids:
        stations = sorted(stations, key=lambda item: normalize_text(item.get("name") or item.get("station_id")).lower())
    lines = []

    for index, station in enumerate(stations, start=1):
        summary = station_record_summary(registry, station.get("station_id"))
        append_station_markdown_block(lines, station, summary, index)

    if not lines:
        lines.append("暂无站点")

    return {
        "count": len(stations),
        "text": "\n".join(lines).strip(),
    }


def build_rank_station_markdown(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    rank_result = rank_records(registry, query)
    limit = int(query.get("limit") or 10)
    station_ids = []
    seen_station_ids = set()

    for item in rank_result.get("items", []):
        station_id = normalize_text(item.get("station_id"))
        if not station_id or station_id in seen_station_ids:
            continue
        seen_station_ids.add(station_id)
        station_ids.append(station_id)
        if len(station_ids) >= limit:
            break

    stations_by_id = {
        normalize_text(station.get("station_id")): station
        for station in registry.get("stations", [])
    }
    stations = [stations_by_id[station_id] for station_id in station_ids if station_id in stations_by_id]

    lines = []

    for index, station in enumerate(stations, start=1):
        append_station_markdown_block(lines, station, station_record_summary(registry, station.get("station_id")), index)

    if not lines:
        lines.append("暂无站点")

    return {
        "count": len(stations),
        "text": "\n".join(lines).strip(),
        "station_ids": station_ids,
    }


def build_leaderboard_copy(rank_result: Dict[str, Any]) -> Dict[str, Any]:
    filters = rank_result.get("filters", {})
    items = rank_result.get("items", [])
    metric = filters.get("metric") or "summary_rmb_per_m"
    model_name = filters.get("model_name") or "全部模型"
    group = filters.get("group") or "全部分组"
    direction = filters.get("direction") or "asc"

    metric_titles = {
        "summary_rmb_per_m": "综合成本",
        "input_rmb_per_m": "输入成本",
        "output_rmb_per_m": "输出成本",
        "cache_read_rmb_per_m": "缓存读取成本",
        "cache_write_rmb_per_m": "缓存创建成本",
        "output_input_ratio": "输出/输入比",
        "cache_read_discount_vs_input": "缓存读取折扣",
    }
    metric_title = metric_titles.get(metric, metric)
    sort_text = "从低到高" if direction == "asc" else "从高到低"

    header = f"{model_name} / {group} 排行榜（按{metric_title}{sort_text}）"
    lines = [header]

    for index, item in enumerate(items, start=1):
        station_name = item.get("station_name") or item.get("station_id")
        alias = ""
        aliases = item.get("aliases") or []
        if aliases:
            alias = f" [{aliases[0]}]"
        website = item.get("website") or ""
        lines.append(
            f"{index}. {station_name}{alias} - {metric_title}: {item.get('value')} - {item.get('copy_text')}"
            + (f" - {website}" if website else "")
        )

    summary = None
    if items:
        best = items[0]
        summary = (
            f"当前最优：{best.get('station_name')}，{metric_title}={best.get('value')}，"
            f"文案：{best.get('copy_text')}"
        )

    return {
        "title": header,
        "summary": summary,
        "lines": lines,
        "text": "\n".join(lines + ([summary] if summary else [])),
    }


def cleanup_test_stations(registry: Dict[str, Any]) -> Dict[str, Any]:
    removed_station_ids = [station.get("station_id") for station in registry.get("stations", []) if is_test_station(station)]
    removed_stations = [station for station in registry.get("stations", []) if is_test_station(station)]
    if not removed_station_ids:
        return {
            "removed_station_count": 0,
            "removed_record_count": 0,
            "removed_stations": [],
        }

    registry["stations"] = [station for station in registry.get("stations", []) if station.get("station_id") not in removed_station_ids]
    removed_records = [record for record in registry.get("price_records", []) if record.get("station_id") in removed_station_ids]
    registry["price_records"] = [
        record for record in registry.get("price_records", []) if record.get("station_id") not in removed_station_ids
    ]
    save_registry(registry)
    return {
        "removed_station_count": len(removed_stations),
        "removed_record_count": len(removed_records),
        "removed_stations": [
            {
                "station_id": station.get("station_id"),
                "name": station.get("name"),
            }
            for station in removed_stations
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage site pricing registry.")
    parser.add_argument(
        "command",
        choices=[
            "search",
            "quick",
            "upsert",
            "rank",
            "rank-stations-md",
            "list",
            "leaderboard",
            "update-station",
            "patch-record",
            "stations-md",
            "cleanup-test",
        ],
        help="Registry action",
    )
    parser.add_argument("--json-file", required=True, help="Path to the JSON payload file")
    args = parser.parse_args()

    payload = load_json_file(args.json_file)
    registry = load_registry()

    if args.command == "search":
        result = search_registry(registry, payload)
    elif args.command == "quick":
        result = quick_query(registry, payload)
    elif args.command == "upsert":
        result = upsert_record(registry, payload)
    elif args.command == "rank":
        result = rank_records(registry, payload)
    elif args.command == "rank-stations-md":
        result = build_rank_station_markdown(registry, payload)
    elif args.command == "leaderboard":
        result = build_leaderboard_copy(rank_records(registry, payload))
    elif args.command == "update-station":
        result = update_station_fields(registry, payload)
    elif args.command == "patch-record":
        result = patch_record_fields(registry, payload)
    elif args.command == "stations-md":
        result = build_station_markdown(registry, payload)
    elif args.command == "cleanup-test":
        result = cleanup_test_stations(registry)
    else:
        result = list_registry(registry, payload)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
