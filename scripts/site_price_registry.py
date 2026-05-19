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


def build_station_identifiers(station: Dict[str, Any]) -> List[str]:
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
            "aliases": unique_strings(build_station_identifiers(station_payload)),
            "website": normalize_text(station_payload.get("website")),
            "api_base_url": normalize_text(station_payload.get("api_base_url") or station_payload.get("api_url")),
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
    station["api_base_url"] = normalize_text(
        station_payload.get("api_base_url") or station_payload.get("api_url") or station.get("api_base_url")
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

    if "api_base_url" in station_payload or "api_url" in station_payload:
        value = normalize_text(station_payload.get("api_base_url") or station_payload.get("api_url"))
        if value and value != station.get("api_base_url"):
            station["api_base_url"] = value
            changed_fields.append("api_base_url")

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

    new_aliases = unique_strings(station.get("aliases", []) + build_station_identifiers(station_payload))
    if new_aliases != station.get("aliases", []):
        station["aliases"] = new_aliases
        changed_fields.append("aliases")

    station["updated_at"] = timestamp
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


def build_rank_item(registry: Dict[str, Any], record: Dict[str, Any], metric: str) -> Optional[Dict[str, Any]]:
    computed = record.get("computed", {})
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

    station = next((item for item in registry.get("stations", []) if item.get("station_id") == record.get("station_id")), {})
    return {
        "station_id": record.get("station_id"),
        "station_name": station.get("name") or record.get("station_id"),
        "aliases": station.get("aliases", []),
        "website": station.get("website"),
        "api_base_url": station.get("api_base_url"),
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
    keyword = station_id or normalize_text(station.get("name") or station.get("website") or station.get("api_base_url"))
    return build_station_markdown(registry, {"keyword": keyword})


def upsert_record(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    station_payload = deepcopy(payload.get("station") or {})
    pricing_payload = deepcopy(payload.get("pricing") or {})

    if not station_payload:
        raise ValueError("缺少 station 对象")
    if not pricing_payload:
        raise ValueError("缺少 pricing 对象")

    station = upsert_station(registry, station_payload)
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
        "recharge_ratio": pricing_payload.get("recharge_ratio") or pricing_payload.get("充值比") or "1:1",
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
        "recharge_ratio": ["recharge_ratio", "充值比"],
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
        "recharge_ratio": record.get("recharge_ratio"),
        "sale_price": record.get("sale_price"),
    }

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
            "recharge_ratio": pricing_payload.get("recharge_ratio"),
            "sale_price": pricing_payload.get("sale_price"),
            "computed": recomputed,
            "updated_at": now_iso(),
        }
    )
    registry["price_records"][record_index] = record
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
    if normalize_text(station.get("api_base_url")):
        score += 1
    if normalize_text(station.get("notes")):
        score += 1

    if score == 3:
        return "完整"
    if score == 2:
        return "较完整"
    if score == 1:
        return "部分缺失"
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


def build_computed_price_text(record: Dict[str, Any]) -> str:
    computed = record.get("computed", {})
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


def build_station_markdown(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    keyword = normalize_text(query.get("keyword"))
    stations = registry.get("stations", [])
    if keyword:
        lowered = keyword.lower()
        filtered = []
        for station in stations:
            haystacks = [
                station.get("station_id"),
                station.get("name"),
                station.get("website"),
                station.get("api_base_url"),
                *(station.get("aliases") or []),
            ]
            if any(lowered in normalize_text(item).lower() for item in haystacks):
                filtered.append(station)
        stations = filtered

    stations = sorted(stations, key=lambda item: normalize_text(item.get("name") or item.get("station_id")).lower())
    test_station_count = sum(1 for station in stations if is_test_station(station))
    real_station_count = len(stations) - test_station_count
    lines = []
    lines.append("# 中转站清单")
    lines.append("")
    lines.append(f"- 站点总数：`{len(stations)}`")
    lines.append(f"- 正式站点：`{real_station_count}`")
    lines.append(f"- 测试站点：`{test_station_count}`")
    lines.append(f"- 价格记录总数：`{len(registry.get('price_records', []))}`")
    if keyword:
        lines.append(f"- 过滤关键字：`{keyword}`")
    lines.append("")

    for index, station in enumerate(stations, start=1):
        summary = station_record_summary(registry, station.get("station_id"))
        aliases = station.get("aliases") or []
        alias_text = " / ".join(aliases[:5]) if aliases else "未记录"
        website = normalize_text(station.get("website")) or "未记录"
        api_base_url = normalize_text(station.get("api_base_url")) or "未记录"
        notes = normalize_text(station.get("notes")) or "无"
        marker = "（测试）" if is_test_station(station) else ""

        lines.append(f"## {index}. {station.get('name') or station.get('station_id')}{marker}")
        lines.append("")
        lines.append(f"- 官网：{website}")
        lines.append(f"- API：{api_base_url}")
        lines.append(f"- 备注：{notes}")
        if is_test_station(station):
            lines.append("- 标识：测试数据")
        lines.append(f"- 创建时间：`{iso_to_display(station.get('created_at'))}`")
        lines.append(f"- 最后更新：`{iso_to_display(station.get('updated_at'))}`")
        lines.append("")
        lines.append("| 模型 | 分组 | 分组备注 | 倍率 | 折算价格 | 综合价 |")
        lines.append("|---|---|---|---:|---|---:|")
        for record in summary["records"]:
            computed = record.get("computed", {})
            multiplier = trim_decimal_text(computed.get("multiplier") or record.get("multiplier") or "1")
            group_note = normalize_text(record.get("group_note")) or "-"
            summary_cost = format_rmb_per_m(computed.get("summary", {}).get("rmb_per_m") or "-")
            lines.append(
                f"| `{record.get('model_name')}` | `{record.get('group')}` | {group_note} | `{multiplier}` | "
                f"{build_computed_price_text(record)} | `{summary_cost}` |"
            )
        if not summary["records"]:
            lines.append("| - | - | - | - | 暂无价格记录 | - |")
        lines.append("")

    return {
        "count": len(stations),
        "text": "\n".join(lines).strip(),
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
            "upsert",
            "rank",
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
    elif args.command == "upsert":
        result = upsert_record(registry, payload)
    elif args.command == "rank":
        result = rank_records(registry, payload)
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
