#!/usr/bin/env python
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from calc_model_price import apply_official_model_defaults, compute, format_decimal, to_decimal
from model_catalog import canonical_model_name
from mysql_storage import mysql_enabled, load_history as load_mysql_history, load_registry as load_mysql_registry
from mysql_storage import save_history as save_mysql_history, save_registry as save_mysql_registry


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "assets" / "site-price-registry.json"
HISTORY_PATH = Path(__file__).resolve().parent.parent / "assets" / "site-price-history.json"
DASHBOARD_HTML_PATH = Path(__file__).resolve().parent.parent / "runtime" / "site-price-dashboard.html"
DASHBOARD_META_PATH = Path(__file__).resolve().parent.parent / "runtime" / "site-price-dashboard.meta.json"
DEFAULT_STALE_AFTER_DAYS = 30
LOW_CONFIDENCE_THRESHOLD = 0.7
ANOMALY_LOW_RATIO = 0.2
CONFIDENCE_SOURCE_RULES = [
    (("official", "官网", "官方", "控制台", "价格页"), 0.95, "官网/官方价格页"),
    (("screenshot", "截图", "图片", "price-card", "价格卡片"), 0.85, "截图/价格卡片"),
    (("qq", "qq群", "群公告", "微信群", "wx群", "公告"), 0.70, "社群公告"),
    (("user", "manual", "用户口述", "手动", "口述"), 0.60, "用户口述/手动记录"),
    (("inferred", "history", "历史推断", "推断", "默认"), 0.40, "历史推断/默认值"),
]
PRICE_HISTORY_FIELDS = [
    "input_price",
    "output_price",
    "cache_price",
    "cache_read_price",
    "cache_write_price",
    "multiplier",
    "recharge_ratio",
    "sale_price",
    "computed",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_detection_flag(value: Any) -> Optional[bool]:
    if value is None:
        return None
    text = normalize_text(value).lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "是", "已检测"}:
        return True
    if text in {"0", "false", "no", "n", "否", "未检测"}:
        return False
    return None


def station_payload_mentions_checked(station_payload: Dict[str, Any]) -> bool:
    detection_flag = normalize_detection_flag(
        station_payload.get("is_checked")
        or station_payload.get("是否已检测")
        or station_payload.get("checked")
        or station_payload.get("检测状态")
    )
    if detection_flag is True:
        return True
    return "已检测" in normalize_text(station_payload.get("notes"))


def load_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


def load_registry() -> Dict[str, Any]:
    if mysql_enabled():
        return load_mysql_registry()
    if not REGISTRY_PATH.exists():
        return {"version": 1, "stations": [], "price_records": []}
    return load_json_file(str(REGISTRY_PATH))


def save_registry(registry: Dict[str, Any]) -> None:
    if mysql_enabled():
        save_mysql_registry(registry)
        return
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as file:
        json.dump(registry, file, ensure_ascii=False, indent=2)


def load_history() -> Dict[str, Any]:
    if mysql_enabled():
        return load_mysql_history()
    if not HISTORY_PATH.exists():
        return {"version": 1, "changes": []}
    return load_json_file(str(HISTORY_PATH))


def save_history(history: Dict[str, Any]) -> None:
    if mysql_enabled():
        save_mysql_history(history)
        return
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as file:
        json.dump(history, file, ensure_ascii=False, indent=2)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = normalize_text(value).lower()
    return text in {"1", "true", "yes", "y", "是", "测试", "test"}


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = normalize_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def days_since(value: Any, now: Optional[datetime] = None) -> Optional[int]:
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max((current - parsed).days, 0)


def normalize_int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return numeric if numeric > 0 else default


def normalize_confidence(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    text = normalize_text(value).rstrip("%")
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        return None
    if numeric > 1 and numeric <= 100:
        numeric = numeric / 100
    if numeric < 0 or numeric > 1:
        return None
    return round(numeric, 2)


def normalize_key(value: Any) -> str:
    text = normalize_text(value).lower()
    text = re.sub(r"^https?://", "", text)
    text = text.rstrip("/")
    text = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE)
    text = text.replace("_", "-")
    return text.strip("-")


def compact_search_key(value: Any) -> str:
    return re.sub(r"[-\s]+", "", normalize_key(value))


def fuzzy_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def text_matches_query(query: Any, value: Any) -> bool:
    query_text = normalize_text(query).lower()
    value_text = normalize_text(value).lower()
    if not query_text or not value_text:
        return False
    if query_text in value_text:
        return True

    query_key = normalize_key(query_text)
    value_key = normalize_key(value_text)
    if query_key and value_key and query_key in value_key:
        return True

    query_compact = compact_search_key(query_text)
    value_compact = compact_search_key(value_text)
    if query_compact and value_compact and query_compact in value_compact:
        return True

    query_parts = [part for part in re.split(r"[-\s]+", query_key) if part]
    if query_parts and all(part in value_key for part in query_parts):
        return True

    if len(query_compact) >= 4:
        candidates = [part for part in re.split(r"[-\s:/._]+", value_key) if len(part) >= 4]
        candidates.append(value_compact)
        if any(fuzzy_ratio(query_compact, candidate) >= 0.78 for candidate in candidates):
            return True
    return False


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


def resolve_confidence(payload: Dict[str, Any], station: Optional[Dict[str, Any]] = None) -> Tuple[float, str]:
    explicit = normalize_confidence(
        payload.get("confidence_score")
        or payload.get("可信度")
        or payload.get("price_confidence")
    )
    if explicit is not None:
        return explicit, "显式指定"

    haystacks = [
        payload.get("source"),
        payload.get("来源"),
        payload.get("confidence_reason"),
        payload.get("notes"),
        payload.get("备注"),
        payload.get("group_note"),
        payload.get("分组备注"),
    ]
    if station:
        haystacks.extend([station.get("notes"), station.get("website")])
    text = " ".join(normalize_text(item).lower() for item in haystacks if normalize_text(item))
    for keywords, score, reason in CONFIDENCE_SOURCE_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            return score, reason
    return 0.60, "默认手动记录"


def record_confidence(record: Dict[str, Any], station: Optional[Dict[str, Any]] = None) -> Tuple[float, str]:
    explicit = normalize_confidence(record.get("confidence_score"))
    if explicit is not None:
        return explicit, normalize_text(record.get("confidence_reason")) or "记录字段"
    return resolve_confidence(record, station)


def confidence_label(score: Any) -> str:
    numeric = normalize_confidence(score)
    if numeric is None:
        return "-"
    return f"{numeric:.2f}"


def resolve_stale_after_days(station: Dict[str, Any], record: Dict[str, Any]) -> int:
    return normalize_int(
        record.get("stale_after_days")
        or station.get("stale_after_days"),
        DEFAULT_STALE_AFTER_DAYS,
    )


def record_verified_at(station: Dict[str, Any], record: Dict[str, Any]) -> str:
    return (
        normalize_text(record.get("last_verified_at"))
        or normalize_text(station.get("last_verified_at"))
        or normalize_text(record.get("updated_at"))
        or normalize_text(record.get("created_at"))
    )


def price_stale_warnings(station: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
    warnings = []
    now = datetime.now(timezone.utc)
    expires_at = parse_iso_datetime(record.get("expires_at") or station.get("expires_at"))
    if expires_at is not None and expires_at < now:
        warnings.append(f"价格已过期（{iso_to_display(expires_at.isoformat())}）")

    verified_at = record_verified_at(station, record)
    age_days = days_since(verified_at, now)
    stale_after_days = resolve_stale_after_days(station, record)
    if age_days is None:
        warnings.append("未记录验证时间")
    elif age_days > stale_after_days:
        warnings.append(f"该价格 {age_days} 天未验证，可能已过期")
    return warnings


def record_health_warnings(station: Dict[str, Any], record: Dict[str, Any], extra: Optional[List[str]] = None) -> List[str]:
    warnings = []
    confidence_score, _ = record_confidence(record, station)
    if confidence_score < LOW_CONFIDENCE_THRESHOLD:
        warnings.append(f"可信度偏低（{confidence_label(confidence_score)}）")
    warnings.extend(price_stale_warnings(station, record))
    warnings.extend(extra or [])
    return unique_strings(warnings)


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
    identifiers.extend(ensure_list(station.get("station_id")))
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
            station.get("station_id"),
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


def find_station_candidates(registry: Dict[str, Any], station_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = []
    for station in registry.get("stations", []):
        score = station_match_score(station, station_payload)
        if score <= 0:
            continue
        candidates.append(
            {
                "station": station,
                "score": score,
                "station_id": station.get("station_id"),
                "name": station.get("name"),
                "website": station.get("website"),
                "aliases": station.get("aliases", []),
            }
        )
    candidates.sort(key=lambda item: (-item["score"], normalize_text(item.get("name") or item.get("station_id")).lower()))
    return candidates


def station_conflict_result(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "needs_confirmation": True,
        "message": f"我找到 {len(candidates)} 个可能的站点，请回复编号。",
        "candidates": [
            {
                "index": index,
                "station_id": item.get("station_id"),
                "name": item.get("name"),
                "website": item.get("website"),
                "score": item.get("score"),
            }
            for index, item in enumerate(candidates, start=1)
        ],
    }


def resolve_station_for_write(
    registry: Dict[str, Any],
    station_payload: Dict[str, Any],
    allow_create: bool = False,
) -> Tuple[Optional[Dict[str, Any]], int, Optional[Dict[str, Any]]]:
    candidates = find_station_candidates(registry, station_payload)
    if not candidates:
        return None, 0, None

    best = candidates[0]
    top_score = best["score"]
    ambiguous = [
        item
        for item in candidates
        if item["score"] == top_score
        or (top_score <= 2 and item["score"] >= top_score - 1)
    ]
    if len(ambiguous) > 1 and not allow_create:
        return None, top_score, station_conflict_result(ambiguous[:5])
    return best["station"], top_score, None


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
    detection_flag = normalize_detection_flag(
        station_payload.get("is_checked")
        or station_payload.get("是否已检测")
        or station_payload.get("checked")
        or station_payload.get("检测状态")
    )
    auto_checked = station_payload_mentions_checked(station_payload)
    checked_at_value = normalize_text(station_payload.get("checked_at") or station_payload.get("检测时间"))
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
            "invite_url": normalize_text(station_payload.get("invite_url") or station_payload.get("邀请链接")),
            "is_checked": detection_flag if detection_flag is not None else auto_checked,
            "checked_at": checked_at_value or (timestamp if (detection_flag is True or auto_checked) else ""),
            "recharge_ratio": normalize_text(station_payload.get("recharge_ratio") or station_payload.get("充值比")) or "1:1",
            "group_multipliers": normalize_group_multiplier_map(station_payload.get("group_multipliers")),
            "is_test_data": normalize_bool(station_payload.get("is_test_data")),
            "notes": normalize_text(station_payload.get("notes")),
            "last_verified_at": normalize_text(station_payload.get("last_verified_at") or station_payload.get("最后验证时间")),
            "stale_after_days": normalize_int(station_payload.get("stale_after_days") or station_payload.get("过期天数"), DEFAULT_STALE_AFTER_DAYS),
            "expires_at": normalize_text(station_payload.get("expires_at") or station_payload.get("过期时间")),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        registry.setdefault("stations", []).append(station)
        return station

    station["name"] = normalize_text(station_payload.get("name") or station.get("name"))
    station["website"] = normalize_text(station_payload.get("website") or station.get("website"))
    station["invite_url"] = normalize_text(
        station_payload.get("invite_url") or station_payload.get("邀请链接") or station.get("invite_url")
    )
    if detection_flag is not None:
        station["is_checked"] = detection_flag
        if detection_flag:
            station["checked_at"] = checked_at_value or normalize_text(station.get("checked_at")) or timestamp
        else:
            station["checked_at"] = checked_at_value or ""
    elif auto_checked:
        station["is_checked"] = True
        station["checked_at"] = checked_at_value or normalize_text(station.get("checked_at")) or timestamp
    elif checked_at_value:
        station["checked_at"] = checked_at_value
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
    if "last_verified_at" in station_payload or "最后验证时间" in station_payload:
        station["last_verified_at"] = normalize_text(station_payload.get("last_verified_at") or station_payload.get("最后验证时间"))
    if "stale_after_days" in station_payload or "过期天数" in station_payload:
        station["stale_after_days"] = normalize_int(
            station_payload.get("stale_after_days") or station_payload.get("过期天数"),
            DEFAULT_STALE_AFTER_DAYS,
        )
    if "expires_at" in station_payload or "过期时间" in station_payload:
        station["expires_at"] = normalize_text(station_payload.get("expires_at") or station_payload.get("过期时间"))
    station["aliases"] = unique_strings(station.get("aliases", []) + build_station_identifiers(station_payload))
    station["updated_at"] = timestamp
    return station


def update_station_fields(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    station_payload = deepcopy(payload.get("station") or payload)
    station, score, conflict = resolve_station_for_write(registry, station_payload)
    if conflict:
        return conflict
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

    if "invite_url" in station_payload or "邀请链接" in station_payload:
        value = normalize_text(station_payload.get("invite_url") or station_payload.get("邀请链接"))
        if value != normalize_text(station.get("invite_url")):
            station["invite_url"] = value
            changed_fields.append("invite_url")

    detection_flag = normalize_detection_flag(
        station_payload.get("is_checked")
        or station_payload.get("是否已检测")
        or station_payload.get("checked")
        or station_payload.get("检测状态")
    )
    auto_checked = station_payload_mentions_checked(station_payload)
    checked_at_value = normalize_text(station_payload.get("checked_at") or station_payload.get("检测时间"))
    if detection_flag is not None:
        if detection_flag != bool(station.get("is_checked")):
            station["is_checked"] = detection_flag
            changed_fields.append("is_checked")
        target_checked_at = checked_at_value or (timestamp if detection_flag else "")
        if normalize_text(station.get("checked_at")) != target_checked_at:
            station["checked_at"] = target_checked_at
            changed_fields.append("checked_at")
    elif auto_checked:
        if not bool(station.get("is_checked")):
            station["is_checked"] = True
            changed_fields.append("is_checked")
        target_checked_at = checked_at_value or normalize_text(station.get("checked_at")) or timestamp
        if normalize_text(station.get("checked_at")) != target_checked_at:
            station["checked_at"] = target_checked_at
            changed_fields.append("checked_at")
    elif checked_at_value and normalize_text(station.get("checked_at")) != checked_at_value:
        station["checked_at"] = checked_at_value
        changed_fields.append("checked_at")

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

    if "last_verified_at" in station_payload or "最后验证时间" in station_payload:
        value = normalize_text(station_payload.get("last_verified_at") or station_payload.get("最后验证时间"))
        if value != normalize_text(station.get("last_verified_at")):
            station["last_verified_at"] = value
            changed_fields.append("last_verified_at")

    if "stale_after_days" in station_payload or "过期天数" in station_payload:
        value = normalize_int(station_payload.get("stale_after_days") or station_payload.get("过期天数"), DEFAULT_STALE_AFTER_DAYS)
        if value != resolve_stale_after_days(station, {}):
            station["stale_after_days"] = value
            changed_fields.append("stale_after_days")

    if "expires_at" in station_payload or "过期时间" in station_payload:
        value = normalize_text(station_payload.get("expires_at") or station_payload.get("过期时间"))
        if value != normalize_text(station.get("expires_at")):
            station["expires_at"] = value
            changed_fields.append("expires_at")

    new_aliases = unique_strings(station.get("aliases", []) + build_station_identifiers(station_payload, include_api=False))
    if new_aliases != station.get("aliases", []):
        station["aliases"] = new_aliases
        changed_fields.append("aliases")

    station["updated_at"] = timestamp
    if "recharge_ratio" in changed_fields:
        recompute_station_records(registry, station, timestamp)
    save_registry(registry)
    return refresh_dashboard_after_write(registry, {
        "_skip_dashboard_refresh": normalize_bool(payload.get("_skip_dashboard_refresh")),
        "station": station,
        "changed_fields": unique_strings(changed_fields),
        "station_snapshot": build_station_snapshot(registry, station),
    })


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
        append_price_history(record, refreshed_record, "station_recharge_ratio_changed")
        registry["price_records"][index] = refreshed_record


def history_value(record: Dict[str, Any]) -> Dict[str, Any]:
    return {field: deepcopy(record.get(field)) for field in PRICE_HISTORY_FIELDS if field in record}


def price_record_changed(old_record: Dict[str, Any], new_record: Dict[str, Any]) -> bool:
    return history_value(old_record) != history_value(new_record)


def record_summary_price(record: Dict[str, Any]) -> Optional[Any]:
    computed = record.get("computed") if isinstance(record.get("computed"), dict) else {}
    return computed.get("summary", {}).get("rmb_per_m") if isinstance(computed.get("summary"), dict) else None


def calculate_change_percent(old_record: Dict[str, Any], new_record: Dict[str, Any]) -> Optional[str]:
    old_price = to_decimal(record_summary_price(old_record))
    new_price = to_decimal(record_summary_price(new_record))
    if old_price is None or new_price is None or old_price == 0:
        return None
    percent = (new_price - old_price) / old_price * 100
    return format_decimal(percent)


def append_price_history(
    old_record: Dict[str, Any],
    new_record: Dict[str, Any],
    source: str,
    changed_fields: Optional[List[str]] = None,
) -> None:
    if not price_record_changed(old_record, new_record):
        return
    history = load_history()
    history.setdefault("version", 1)
    history.setdefault("changes", []).append(
        {
            "changed_at": now_iso(),
            "source": source,
            "record_id": new_record.get("record_id") or old_record.get("record_id"),
            "station_id": new_record.get("station_id") or old_record.get("station_id"),
            "model_name": new_record.get("model_name") or old_record.get("model_name"),
            "group": new_record.get("group") or old_record.get("group"),
            "changed_fields": changed_fields or [
                field
                for field in PRICE_HISTORY_FIELDS
                if old_record.get(field) != new_record.get(field)
            ],
            "old": history_value(old_record),
            "new": history_value(new_record),
            "summary_change_percent": calculate_change_percent(old_record, new_record),
        }
    )
    save_history(history)


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
        "record_id": record.get("record_id"),
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
        "confidence_score": confidence_label(record_confidence(record, station)[0]),
        "confidence_reason": record_confidence(record, station)[1],
        "last_verified_at": record_verified_at(station, record),
        "stale_after_days": resolve_stale_after_days(station, record),
        "updated_at": record.get("updated_at"),
    }


def record_search_fields(station: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
    values = [
        station.get("station_id"),
        station.get("name"),
        station.get("website"),
        station.get("api_base_url"),
        station.get("api_url"),
        station.get("notes"),
        station.get("recharge_ratio"),
        record.get("model_name"),
        record.get("group"),
        record.get("group_note"),
    ]
    values.extend(station.get("aliases") or [])
    values.extend(ensure_list(record.get("tags")))
    return [normalize_text(value) for value in values if normalize_text(value)]


def record_matches_terms(station: Dict[str, Any], record: Dict[str, Any], terms: List[str], require_all: bool = True) -> bool:
    normalized_terms = [normalize_text(term) for term in terms if normalize_text(term)]
    if not normalized_terms:
        return True
    fields = record_search_fields(station, record)
    matcher = all if require_all else any
    return matcher(any(text_matches_query(term, field) for field in fields) for term in normalized_terms)


def item_explain_parts(station: Dict[str, Any], record: Dict[str, Any], metric: str, computed: Dict[str, Any]) -> List[str]:
    parts = []
    multiplier = to_decimal(computed.get("multiplier") or record.get("multiplier"))
    if multiplier is not None and multiplier < 1:
        parts.append(f"倍率低（{trim_decimal_text(format_decimal(multiplier))}）")

    recharge_ratio = resolve_station_recharge_ratio(station, record)
    ratio_parts = re.split(r"\s*:\s*", recharge_ratio)
    if len(ratio_parts) == 2:
        left = to_decimal(ratio_parts[0])
        right = to_decimal(ratio_parts[1])
        if left is not None and right is not None and left > 0:
            credit_per_rmb = right / left
            if credit_per_rmb > 1:
                parts.append(f"充值比高（{recharge_ratio}）")

    group_note = normalize_text(record.get("group_note"))
    group = normalize_text(record.get("group"))
    if text_matches_query("限时", group) or text_matches_query("限时", group_note):
        parts.append("限时特价")
    if text_matches_query("特价", group) or text_matches_query("特价", group_note):
        parts.append("特价分组")

    dimensions = computed.get("dimensions", {}) if isinstance(computed.get("dimensions"), dict) else {}
    input_price = to_decimal((dimensions.get("input") or {}).get("rmb_per_m"))
    cache_price = to_decimal((dimensions.get("cache_read") or {}).get("rmb_per_m"))
    if cache_price is not None and input_price is not None and input_price > 0 and cache_price < input_price / 5:
        parts.append("缓存读取价低")
    if metric == "output_rmb_per_m":
        parts.append("按输出价排序")
    elif metric == "input_rmb_per_m":
        parts.append("按输入价排序")
    elif metric == "cache_read_rmb_per_m":
        parts.append("按缓存读取价排序")
    return unique_strings(parts)[:3]


def median_decimal(values: List[Any]) -> Optional[Any]:
    decimals = sorted(value for value in (to_decimal(item) for item in values) if value is not None)
    if not decimals:
        return None
    middle = len(decimals) // 2
    if len(decimals) % 2:
        return decimals[middle]
    return (decimals[middle - 1] + decimals[middle]) / 2


def model_summary_medians(registry: Dict[str, Any]) -> Dict[str, Any]:
    values_by_model: Dict[str, List[Any]] = {}
    stations_by_id = {
        normalize_text(station.get("station_id")): station
        for station in registry.get("stations", [])
    }
    for record in registry.get("price_records", []):
        station = stations_by_id.get(normalize_text(record.get("station_id")), {})
        try:
            computed = compute(build_record_pricing_payload(station, record))
        except Exception:
            continue
        model_key = normalize_key(record.get("model_name"))
        summary = computed.get("summary", {}).get("rmb_per_m")
        if model_key and to_decimal(summary) is not None:
            values_by_model.setdefault(model_key, []).append(summary)
    return {
        model_key: median_decimal(values)
        for model_key, values in values_by_model.items()
        if median_decimal(values) is not None
    }


def anomaly_low_price_warnings(record: Dict[str, Any], computed: Dict[str, Any], medians: Dict[str, Any]) -> List[str]:
    model_key = normalize_key(record.get("model_name"))
    median_value = to_decimal(medians.get(model_key))
    current = to_decimal(computed.get("summary", {}).get("rmb_per_m"))
    if median_value is None or current is None or median_value <= 0:
        return []
    if current <= median_value * to_decimal(ANOMALY_LOW_RATIO):
        return ["价格显著低于同模型中位数，请确认是否为倍率后价格或限时活动"]
    return []


def rank_records(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    model_name = normalize_text(query.get("model_name"))
    group = normalize_text(query.get("group"))
    metric = normalize_text(query.get("sort_by") or query.get("metric") or "summary_rmb_per_m")
    direction = normalize_text(query.get("direction") or "asc").lower()
    include_terms = ensure_list(query.get("include_terms") or query.get("include") or query.get("包含"))
    exclude_terms = ensure_list(query.get("exclude_terms") or query.get("exclude") or query.get("排除"))
    min_confidence = normalize_confidence(query.get("min_confidence") or query.get("最低可信度"))
    medians = model_summary_medians(registry)

    matched = []
    stations_by_id = {
        normalize_text(station.get("station_id")): station
        for station in registry.get("stations", [])
    }
    for record in registry.get("price_records", []):
        if model_name and normalize_key(record.get("model_name")) != normalize_key(model_name):
            continue
        if group and normalize_key(record.get("group")) != normalize_key(group):
            continue
        station = stations_by_id.get(normalize_text(record.get("station_id")), {})
        if include_terms and not record_matches_terms(station, record, include_terms, require_all=True):
            continue
        if exclude_terms and record_matches_terms(station, record, exclude_terms, require_all=False):
            continue
        confidence_score, _ = record_confidence(record, station)
        if min_confidence is not None and confidence_score < min_confidence:
            continue
        item = build_rank_item(registry, record, metric)
        if item is None:
            continue
        computed = compute(build_record_pricing_payload(station, record))
        item["cheap_reasons"] = item_explain_parts(station, record, metric, computed)
        item["warnings"] = record_health_warnings(
            station,
            record,
            anomaly_low_price_warnings(record, computed, medians),
        )
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
            "include_terms": include_terms,
            "exclude_terms": exclude_terms,
            "min_confidence": min_confidence,
        },
        "count": len(matched),
        "items": matched,
    }


def normalize_model_alias(value: Any) -> str:
    return canonical_model_name(value)


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
        if group.lower() in {"pro", "plus"} and re.search(rf"{re.escape(group)}\s*号池", lowered, re.I):
            continue
        if re.search(rf"(?:排除|不要|不看|过滤掉|剔除)[^，。,；;]*{re.escape(group)}", text, re.I):
            continue
        if group.lower() in lowered:
            return group
    return ""


def split_filter_terms(text: str) -> List[str]:
    terms = []
    for part in re.split(r"[,，、/]|和|且|并且", normalize_text(text)):
        term = normalize_text(part)
        term = re.sub(r"^(?:有|带|支持|包含|含|是)\s*", "", term)
        term = re.sub(r"\s*(?:的)?(?:站点|中转站|分组|记录)$", "", term)
        if term:
            terms.append(term)
    return unique_strings(terms)


def extract_quick_filter_terms(text: str) -> Tuple[List[str], List[str]]:
    include_terms: List[str] = []
    exclude_terms: List[str] = []

    for pattern, target in [
        (r"(?:排除|不要|不看|过滤掉|剔除)\s*([^，。,；;]+)", exclude_terms),
        (r"(?:只看|仅看|筛选|包含|含有)\s*([^，。,；;]+)", include_terms),
    ]:
        for match in re.finditer(pattern, text, re.I):
            target.extend(split_filter_terms(match.group(1)))

    descriptor_terms = [
        "pro号池",
        "plus号池",
        "售后群",
        "QQ群",
        "微信群",
        "v2ex",
        "gpt-image",
        "支持图片",
        "限时特价",
        "特价",
        "不稳定",
        "稳定",
    ]
    lowered = text.lower()
    for term in descriptor_terms:
        if term.lower() not in lowered:
            continue
        if term == "稳定" and "不稳定" in lowered:
            continue
        if any(text_matches_query(term, excluded) for excluded in exclude_terms):
            continue
        if re.search(rf"(?:排除|不要|不看|过滤掉|剔除)\s*{re.escape(term)}", text, re.I):
            exclude_terms.append(term)
        elif not any(text_matches_query(term, included) for included in include_terms):
            include_terms.append(term)

    return unique_strings(include_terms), unique_strings(exclude_terms)


def extract_quick_min_confidence(text: str) -> Optional[float]:
    explicit = re.search(r"(?:可信度|confidence)\s*(?:>=|大于|至少|不低于|:|：)?\s*(\d+(?:\.\d+)?%?)", text, re.I)
    if explicit:
        return normalize_confidence(explicit.group(1))
    if re.search(r"排除低可信|不要低可信|过滤低可信|只看可信|高可信|可信价格", text, re.I):
        return LOW_CONFIDENCE_THRESHOLD
    return None


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
    detected_include_terms, detected_exclude_terms = extract_quick_filter_terms(text)
    include_terms = ensure_list(payload.get("include_terms") or payload.get("include") or payload.get("包含")) or detected_include_terms
    exclude_terms = ensure_list(payload.get("exclude_terms") or payload.get("exclude") or payload.get("排除")) or detected_exclude_terms
    min_confidence = normalize_confidence(payload.get("min_confidence") or payload.get("最低可信度"))
    if min_confidence is None:
        min_confidence = extract_quick_min_confidence(text)
    return {
        "model_name": model_name,
        "group": group,
        "sort_by": metric,
        "direction": direction,
        "limit": limit,
        "include_terms": include_terms,
        "exclude_terms": exclude_terms,
        "min_confidence": min_confidence,
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
        ("邀请链接", normalize_text(station.get("invite_url")), 5),
        ("是否已检测", "已检测" if station.get("is_checked") else "未检测", 4),
        ("检测时间", normalize_text(station.get("checked_at")), 3),
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
            matched = text_matches_query(keyword, value)
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
        lines.append(f"邀请链接：{normalize_text(station.get('invite_url')) or '未记录'}")
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

    _, _, conflict = resolve_station_for_write(registry, station_payload)
    if conflict:
        return conflict

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
    metadata_payload = {
        **payload,
        **pricing_payload,
        "notes": normalize_text(payload.get("notes") or station.get("notes")),
        "source": payload.get("source") or pricing_payload.get("source") or "manual",
    }
    confidence_score, confidence_reason = resolve_confidence(metadata_payload, station)
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
        "confidence_score": confidence_score,
        "confidence_reason": normalize_text(payload.get("confidence_reason") or pricing_payload.get("confidence_reason")) or confidence_reason,
        "last_verified_at": (
            normalize_text(payload.get("last_verified_at") or pricing_payload.get("last_verified_at") or payload.get("最后验证时间"))
            or timestamp
        ),
        "stale_after_days": normalize_int(
            payload.get("stale_after_days") or pricing_payload.get("stale_after_days") or payload.get("过期天数"),
            resolve_stale_after_days(station, inherited_record or {}),
        ),
        "expires_at": normalize_text(payload.get("expires_at") or pricing_payload.get("expires_at") or payload.get("过期时间")),
        "computed": computed,
        "updated_at": timestamp,
    }

    record_index = find_record_index(registry, station["station_id"], computed["model_name"], group)
    if record_index is None:
        record["created_at"] = timestamp
        registry.setdefault("price_records", []).append(record)
        action = "created"
    else:
        old_record = deepcopy(registry["price_records"][record_index])
        record["created_at"] = old_record.get("created_at", timestamp)
        if not record.get("expires_at"):
            record["expires_at"] = old_record.get("expires_at", "")
        registry["price_records"][record_index] = record
        append_price_history(old_record, record, "upsert")
        action = "updated"

    station["updated_at"] = timestamp
    save_registry(registry)
    return refresh_dashboard_after_write(registry, {
        "_skip_dashboard_refresh": normalize_bool(payload.get("_skip_dashboard_refresh")),
        "action": action,
        "station": station,
        "record": record,
        "station_snapshot": build_station_snapshot(registry, station),
    })


def patch_record_fields(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    station_payload = deepcopy(payload.get("station") or {})
    station, score, conflict = resolve_station_for_write(registry, station_payload)
    if conflict:
        return conflict
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
    metadata_field_map = {
        "source": ["source", "来源"],
        "confidence_score": ["confidence_score", "可信度", "price_confidence"],
        "confidence_reason": ["confidence_reason", "可信度说明"],
        "last_verified_at": ["last_verified_at", "最后验证时间"],
        "stale_after_days": ["stale_after_days", "过期天数"],
        "expires_at": ["expires_at", "过期时间"],
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

    metadata_updates: Dict[str, Any] = {}
    for target_field, source_keys in metadata_field_map.items():
        for source_key in source_keys:
            if source_key not in payload:
                continue
            value = payload[source_key]
            if target_field == "confidence_score":
                value = normalize_confidence(value)
            elif target_field == "stale_after_days":
                value = normalize_int(value, resolve_stale_after_days(station, record))
            else:
                value = normalize_text(value)
            if record.get(target_field) != value:
                metadata_updates[target_field] = value
                changed_fields.append(target_field)
            break

    if "source" in metadata_updates and "confidence_score" not in metadata_updates:
        score, reason = resolve_confidence({**record, **metadata_updates}, station)
        metadata_updates["confidence_score"] = score
        metadata_updates["confidence_reason"] = reason
        changed_fields.extend(["confidence_score", "confidence_reason"])

    recomputed = compute(pricing_payload)
    timestamp = now_iso()
    if changed_fields and not any(field in metadata_updates for field in ("last_verified_at", "expires_at")):
        metadata_updates["last_verified_at"] = timestamp
    record.update(
        {
            "input_price": pricing_payload.get("input_price"),
            "output_price": pricing_payload.get("output_price"),
            "cache_price": pricing_payload.get("cache_price"),
            "cache_read_price": pricing_payload.get("cache_read_price"),
            "cache_write_price": pricing_payload.get("cache_write_price"),
            "group_note": pricing_payload.get("group_note"),
            "multiplier": pricing_payload.get("multiplier"),
            # patch-record 只应回写明确参与本次计算的充值比，避免站点级字段缺失时误回退到 1:1。
            "recharge_ratio": pricing_payload.get("recharge_ratio") or resolve_station_recharge_ratio(station, record),
            "sale_price": pricing_payload.get("sale_price"),
            "computed": recomputed,
            "updated_at": timestamp,
        }
    )
    record.update(metadata_updates)
    old_record = deepcopy(registry["price_records"][record_index])
    registry["price_records"][record_index] = record
    append_price_history(old_record, record, "patch-record", unique_strings(changed_fields))
    if station_ratio_changed:
        station["updated_at"] = record["updated_at"]
        recompute_station_records(registry, station, record["updated_at"])
    save_registry(registry)

    return refresh_dashboard_after_write(registry, {
        "_skip_dashboard_refresh": normalize_bool(payload.get("_skip_dashboard_refresh")),
        "station": station,
        "record": record,
        "changed_fields": unique_strings(changed_fields),
        "station_snapshot": build_station_snapshot(registry, station),
    })


def delete_records(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    station_payload = deepcopy(payload.get("station") or {})
    station, score, conflict = resolve_station_for_write(registry, station_payload)
    if conflict:
        return conflict
    if station is None or score == 0:
        raise ValueError("未找到可更新的中转站，请先提供已收录的别名、官网或 API 地址")

    model_name = normalize_text(payload.get("model_name") or payload.get("模型名称"))
    group = normalize_text(payload.get("group") or payload.get("分组"))

    kept_records = []
    removed_records = []
    for record in registry.get("price_records", []):
        if record.get("station_id") != station.get("station_id"):
            kept_records.append(record)
            continue
        if model_name and normalize_key(record.get("model_name")) != normalize_key(model_name):
            kept_records.append(record)
            continue
        if group and normalize_key(record.get("group")) != normalize_key(group):
            kept_records.append(record)
            continue
        removed_records.append(deepcopy(record))

    if not removed_records:
        return {
            "removed_count": 0,
            "removed_records": [],
            "station_snapshot": build_station_snapshot(registry, station),
        }

    registry["price_records"] = kept_records
    timestamp = now_iso()
    station["updated_at"] = timestamp
    for record in removed_records:
        append_price_history(record, {}, "delete-record", ["deleted"])
    save_registry(registry)

    return refresh_dashboard_after_write(registry, {
        "_skip_dashboard_refresh": normalize_bool(payload.get("_skip_dashboard_refresh")),
        "station": station,
        "removed_count": len(removed_records),
        "removed_records": [
            {
                "record_id": record.get("record_id"),
                "model_name": record.get("model_name"),
                "group": record.get("group"),
            }
            for record in removed_records
        ],
        "station_snapshot": build_station_snapshot(registry, station),
    })


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


def query_price_history(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    history = load_history()
    station_payload = deepcopy(query.get("station") or query)
    station, score = find_station(registry, station_payload)
    station_id = normalize_text(query.get("station_id") or (station or {}).get("station_id"))
    model_name = normalize_text(query.get("model_name") or query.get("模型名称"))
    group = normalize_text(query.get("group") or query.get("分组"))
    limit = int(query.get("limit") or 20)

    items = []
    for change in history.get("changes", []):
        if station_id and normalize_text(change.get("station_id")) != station_id:
            continue
        if model_name and normalize_key(change.get("model_name")) != normalize_key(model_name):
            continue
        if group and normalize_key(change.get("group")) != normalize_key(group):
            continue
        items.append(change)

    items.sort(key=lambda item: normalize_text(item.get("changed_at")), reverse=True)
    if limit > 0:
        items = items[:limit]
    return {
        "count": len(items),
        "station_match_score": score if station else 0,
        "filters": {
            "station_id": station_id or None,
            "model_name": model_name or None,
            "group": group or None,
        },
        "items": items,
    }


def iso_to_display(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return "未记录"
    parsed = parse_iso_datetime(text)
    if parsed is None:
        return text
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def station_record_summary(registry: Dict[str, Any], station_id: str) -> Dict[str, Any]:
    records = [record for record in registry.get("price_records", []) if record.get("station_id") == station_id]
    model_groups = [f"{record.get('model_name')} / {record.get('group')}" for record in records]
    return {
        "record_count": len(records),
        "model_groups": model_groups,
        "records": records,
    }


def filtered_station_record_summary(
    registry: Dict[str, Any],
    station: Dict[str, Any],
    filters: Optional[Dict[str, Any]] = None,
    rank_items_by_record_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    filters = filters or {}
    rank_items_by_record_id = rank_items_by_record_id or {}
    model_name = normalize_text(filters.get("model_name"))
    group = normalize_text(filters.get("group"))
    include_terms = ensure_list(filters.get("include_terms"))
    exclude_terms = ensure_list(filters.get("exclude_terms"))

    summary = station_record_summary(registry, station.get("station_id"))
    records = []
    for record in summary.get("records", []):
        if model_name and normalize_key(record.get("model_name")) != normalize_key(model_name):
            continue
        if group and normalize_key(record.get("group")) != normalize_key(group):
            continue
        if include_terms and not record_matches_terms(station, record, include_terms, require_all=True):
            continue
        if exclude_terms and record_matches_terms(station, record, exclude_terms, require_all=False):
            continue
        rendered_record = deepcopy(record)
        rank_item = rank_items_by_record_id.get(normalize_text(record.get("record_id")))
        if rank_item:
            rendered_record["_cheap_reasons"] = rank_item.get("cheap_reasons") or []
            rendered_record["_warnings"] = rank_item.get("warnings") or []
            rendered_record["_confidence_score"] = rank_item.get("confidence_score")
            rendered_record["_last_verified_at"] = rank_item.get("last_verified_at")
        records.append(rendered_record)

    return {
        "record_count": len(records),
        "model_groups": [f"{record.get('model_name')} / {record.get('group')}" for record in records],
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
    def display_price(value: Any) -> str:
        text = normalize_text(value)
        if not text:
            return ""
        return re.sub(r"(?i)(/\s*)1M(?:\s*tokens?)?", r"\1M", text)

    parts = []
    if record.get("input_price"):
        parts.append(f"输入 {display_price(record.get('input_price'))}")
    if record.get("output_price"):
        parts.append(f"输出 {display_price(record.get('output_price'))}")
    if record.get("cache_read_price"):
        parts.append(f"缓存读 {display_price(record.get('cache_read_price'))}")
    elif record.get("cache_price"):
        parts.append(f"缓存 {display_price(record.get('cache_price'))}")
    if record.get("cache_write_price"):
        parts.append(f"缓存写 {display_price(record.get('cache_write_price'))}")
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
    invite_url = normalize_text(station.get("invite_url")) or "未记录"
    checked_status = "已检测" if station.get("is_checked") else "未检测"
    checked_at = iso_to_display(station.get("checked_at")) if normalize_text(station.get("checked_at")) else "未记录"
    recharge_ratio = resolve_station_summary_recharge_ratio(station, summary)
    notes = normalize_text(station.get("notes")) or "无"
    marker = "（测试）" if is_test_station(station) else ""

    lines.append(f"{index}. {station.get('name') or station.get('station_id')}{marker}")
    lines.append(f"官网：{website}")
    lines.append(f"邀请链接：{invite_url}")
    lines.append(f"是否已检测：{checked_status}")
    lines.append(f"检测时间：{checked_at}")
    lines.append(f"充值比：{recharge_ratio}")
    lines.append(f"备注：{notes}")
    if is_test_station(station):
        lines.append("标识：测试数据")
    lines.append(f"创建时间：{iso_to_display(station.get('created_at'))}")
    lines.append(f"最后更新：{iso_to_display(station.get('updated_at'))}")
    lines.append("")

    has_group_note = any(normalize_text(record.get("group_note")) for record in summary["records"])
    has_cheap_reason = any(record.get("_cheap_reasons") for record in summary["records"])
    has_quality = any(
        record.get("_warnings")
        or record.get("_confidence_score") not in (None, "")
        or record.get("_last_verified_at")
        or record.get("confidence_score") not in (None, "")
        or record.get("last_verified_at")
        or record.get("expires_at")
        for record in summary["records"]
    )
    headers = ["模型", "分组"]
    aligns = ["---", "---"]
    if has_group_note:
        headers.append("分组备注")
        aligns.append("---")
    headers.extend(["倍率", "折算价格", "综合价"])
    aligns.extend(["---:", "---", "---:"])
    if has_cheap_reason:
        headers.append("便宜原因")
        aligns.append("---")
    if has_quality:
        headers.extend(["可信度", "提醒"])
        aligns.extend(["---:", "---"])
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(aligns) + "|")

    for record in summary["records"]:
        computed = compute(build_record_pricing_payload(station, record))
        multiplier = trim_decimal_text(computed.get("multiplier") or record.get("multiplier") or "1")
        group_note = normalize_text(record.get("group_note")) or "-"
        summary_cost = format_rmb_per_m(computed.get("summary", {}).get("rmb_per_m") or "-")
        cheap_reason = "；".join(record.get("_cheap_reasons") or []) or "-"
        row = [normalize_text(record.get("model_name")), normalize_text(record.get("group"))]
        if has_group_note:
            row.append(group_note)
        row.extend([multiplier, build_computed_price_text(record, computed), summary_cost])
        if has_cheap_reason:
            row.append(cheap_reason)
        if has_quality:
            score, _ = record_confidence(record, station)
            warnings = record.get("_warnings") or record_health_warnings(station, record)
            row.extend([confidence_label(record.get("_confidence_score") or score), "；".join(warnings) or "-"])
        lines.append("| " + " | ".join(row) + " |")

    if not summary["records"]:
        lines.append("| " + " | ".join(["-" for _ in headers]) + " |")
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
    rank_items_by_record_id = {
        normalize_text(item.get("record_id")): item
        for item in rank_result.get("items", [])
        if normalize_text(item.get("record_id"))
    }

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
        summary = filtered_station_record_summary(
            registry,
            station,
            rank_result.get("filters", {}),
            rank_items_by_record_id,
        )
        append_station_markdown_block(lines, station, summary, index)

    if not lines:
        lines.append("暂无站点")

    return {
        "count": len(stations),
        "text": "\n".join(lines).strip(),
        "station_ids": station_ids,
    }


def html_escape(value: Any) -> str:
    return html.escape(normalize_text(value), quote=True)


def html_attr(value: Any) -> str:
    return html_escape(value)


def safe_website_href(value: Any) -> str:
    text = normalize_text(value)
    if re.match(r"^https?://", text, flags=re.I):
        return text
    return ""


def metric_title(metric: Any) -> str:
    titles = {
        "summary_rmb_per_m": "综合成本",
        "input_rmb_per_m": "输入成本",
        "output_rmb_per_m": "输出成本",
        "cache_read_rmb_per_m": "缓存读取成本",
        "cache_write_rmb_per_m": "缓存创建成本",
        "output_input_ratio": "输出/输入比",
        "cache_read_discount_vs_input": "缓存读取折扣",
    }
    return titles.get(normalize_text(metric), normalize_text(metric) or "综合成本")


def format_filter_value(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(normalize_text(item) for item in value if normalize_text(item)) or "无"
    if isinstance(value, float):
        return confidence_label(value)
    return normalize_text(value) or "无"


def collect_station_rows(station: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for record in summary.get("records", []):
        computed = compute(build_record_pricing_payload(station, record))
        score, _ = record_confidence(record, station)
        warnings = record.get("_warnings") or record_health_warnings(station, record)
        rows.append(
            {
                "model_name": normalize_text(record.get("model_name")),
                "group": normalize_text(record.get("group")),
                "group_note": normalize_text(record.get("group_note")) or "-",
                "multiplier": trim_decimal_text(computed.get("multiplier") or record.get("multiplier") or "1"),
                "computed_price": build_computed_price_text(record, computed),
                "summary_cost": format_rmb_per_m(computed.get("summary", {}).get("rmb_per_m") or "-"),
                "cheap_reasons": record.get("_cheap_reasons") or [],
                "confidence": confidence_label(record.get("_confidence_score") or score),
                "warnings": warnings,
            }
        )
    return rows


def station_html_view(station: Dict[str, Any], summary: Dict[str, Any], index: int) -> Dict[str, Any]:
    rows = collect_station_rows(station, summary)
    return {
        "index": index,
        "name": normalize_text(station.get("name") or station.get("station_id")) or "未命名站点",
        "station_id": normalize_text(station.get("station_id")),
        "website": normalize_text(station.get("website")) or "未记录",
        "invite_url": normalize_text(station.get("invite_url")) or "未记录",
        "is_checked": bool(station.get("is_checked")),
        "checked_at": iso_to_display(station.get("checked_at")) if normalize_text(station.get("checked_at")) else "未记录",
        "recharge_ratio": resolve_station_summary_recharge_ratio(station, summary),
        "notes": normalize_text(station.get("notes")) or "无",
        "is_test": is_test_station(station),
        "created_at": iso_to_display(station.get("created_at")),
        "updated_at": iso_to_display(station.get("updated_at")),
        "rows": rows,
    }


def collect_rank_station_html_views(
    registry: Dict[str, Any],
    query: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    rank_result = rank_records(registry, query)
    limit = int(query.get("limit") or 10)
    station_ids = []
    seen_station_ids = set()
    rank_items_by_record_id = {
        normalize_text(item.get("record_id")): item
        for item in rank_result.get("items", [])
        if normalize_text(item.get("record_id"))
    }

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
    views = []
    for index, station_id in enumerate(station_ids, start=1):
        station = stations_by_id.get(station_id)
        if not station:
            continue
        summary = filtered_station_record_summary(
            registry,
            station,
            rank_result.get("filters", {}),
            rank_items_by_record_id,
        )
        views.append(station_html_view(station, summary, index))
    return rank_result, views, station_ids


def collect_station_html_views(registry: Dict[str, Any], query: Dict[str, Any]) -> List[Dict[str, Any]]:
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

    return [
        station_html_view(station, station_record_summary(registry, station.get("station_id")), index)
        for index, station in enumerate(stations, start=1)
    ]


def render_filter_chips(filters: Dict[str, Any]) -> str:
    chips = [
        ("模型", filters.get("model_name") or "全部模型"),
        ("分组", filters.get("group") or "全部分组"),
        ("排序", metric_title(filters.get("metric"))),
        ("方向", "从低到高" if filters.get("direction") == "asc" else "从高到低"),
    ]
    if filters.get("include_terms"):
        chips.append(("包含", filters.get("include_terms")))
    if filters.get("exclude_terms"):
        chips.append(("排除", filters.get("exclude_terms")))
    if filters.get("min_confidence") is not None:
        chips.append(("最低可信度", filters.get("min_confidence")))
    return "".join(
        f'<span class="chip"><b>{html_escape(label)}</b>{html_escape(format_filter_value(value))}</span>'
        for label, value in chips
    )


def render_warning_tags(warnings: List[str]) -> str:
    if not warnings:
        return '<span class="tag tag-ok">稳定</span>'
    tags = []
    for warning in warnings:
        lower = warning.lower()
        level = "risk"
        if "低" in warning or "过期" in warning or "异常" in warning or "low" in lower:
            level = "danger"
        tags.append(f'<span class="tag tag-{level}">{html_escape(warning)}</span>')
    return "".join(tags)


def render_station_cards(views: List[Dict[str, Any]]) -> str:
    if not views:
        return (
            '<section class="empty-state">'
            '<div class="empty-pulse"></div>'
            '<h2>暂无命中站点</h2>'
            '<p>换一个模型、分组或筛选条件后重新生成排行。</p>'
            '</section>'
        )

    cards = []
    for view in views:
        rows = []
        for row in view.get("rows", []):
            reason_text = "；".join(row.get("cheap_reasons") or []) or "-"
            rows.append(
                "<tr>"
                f'<td data-label="模型">{html_escape(row.get("model_name"))}</td>'
                f'<td data-label="分组">{html_escape(row.get("group"))}</td>'
                f'<td data-label="倍率" class="num">{html_escape(row.get("multiplier"))}</td>'
                f'<td data-label="折算价格">{html_escape(row.get("computed_price"))}</td>'
                f'<td data-label="综合价" class="num price">{html_escape(row.get("summary_cost"))}</td>'
                f'<td data-label="便宜原因">{html_escape(reason_text)}</td>'
                f'<td data-label="可信度" class="num">{html_escape(row.get("confidence"))}</td>'
                f'<td data-label="提醒"><div class="tags">{render_warning_tags(row.get("warnings") or [])}</div></td>'
                "</tr>"
            )
        if not rows:
            rows.append(
                '<tr><td data-label="状态" colspan="8" class="empty-row">该站点暂无价格记录</td></tr>'
            )

        website = view.get("website") or "未记录"
        href = safe_website_href(website)
        website_html = html_escape(website)
        if href:
            website_html = f'<a href="{html_attr(href)}" target="_blank" rel="noopener noreferrer">{html_escape(website)}</a>'
        invite_url = view.get("invite_url") or "未记录"
        invite_href = safe_website_href(invite_url)
        invite_html = html_escape(invite_url)
        if invite_href:
            invite_html = f'<a href="{html_attr(invite_href)}" target="_blank" rel="noopener noreferrer">{html_escape(invite_url)}</a>'
        checked_status = "已检测" if view.get("is_checked") else "未检测"
        rank_label = "Prime" if view.get("index") == 1 else f'No.{view.get("index")}'
        test_badge = '<span class="test-badge">测试数据</span>' if view.get("is_test") else ""
        cards.append(
            f'<article class="station-card{" station-card-prime" if view.get("index") == 1 else ""}" '
            f'style="--delay:{min(int(view.get("index") or 1) * 45, 540)}ms">'
            '<div class="card-orbit" aria-hidden="true"></div>'
            '<header class="station-head">'
            f'<div><p class="rank-kicker">{html_escape(rank_label)}</p>'
            f'<h2>{html_escape(view.get("name"))}{test_badge}</h2></div>'
            f'<span class="record-count">{len(view.get("rows") or [])} 条记录</span>'
            '</header>'
            '<div class="station-meta">'
            f'<span><b>官网</b>{website_html}</span>'
            f'<span><b>邀请链接</b>{invite_html}</span>'
            f'<span><b>是否已检测</b>{html_escape(checked_status)}</span>'
            f'<span><b>检测时间</b>{html_escape(view.get("checked_at"))}</span>'
            f'<span><b>充值比</b>{html_escape(view.get("recharge_ratio"))}</span>'
            f'<span><b>最后更新</b>{html_escape(view.get("updated_at"))}</span>'
            '</div>'
            f'<p class="station-notes">{html_escape(view.get("notes"))}</p>'
            '<div class="table-wrap">'
            '<table>'
            '<thead><tr>'
            '<th>模型</th><th>分组</th><th>倍率</th><th>折算价格</th><th>综合价</th><th>便宜原因</th><th>可信度</th><th>提醒</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
            '</div>'
            '</article>'
        )
    return "".join(cards)


def render_station_html_document(
    views: List[Dict[str, Any]],
    payload: Dict[str, Any],
    filters: Dict[str, Any],
    mode: str,
) -> str:
    theme = normalize_text(payload.get("theme") or "dark").lower()
    theme_class = "theme-light" if theme == "light" else "theme-dark"
    title = normalize_text(payload.get("title"))
    if not title:
        title = "站点价格排行雷达" if mode == "rank" else "站点价格情报总览"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    model_text = filters.get("model_name") or "全部模型"
    metric_text = metric_title(filters.get("metric"))
    card_count = len(views)
    record_count = sum(len(view.get("rows") or []) for view in views)
    filter_chips = render_filter_chips(filters)
    cards = render_station_cards(views)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07110f;
      --panel: rgba(10, 26, 23, .78);
      --panel-strong: rgba(13, 37, 33, .92);
      --text: #edf7f3;
      --muted: #9db5ad;
      --line: rgba(137, 255, 219, .16);
      --cyan: #48f2c2;
      --cyan-soft: rgba(72, 242, 194, .18);
      --amber: #ffbf4d;
      --danger: #ff6b7d;
      --risk: #ffdf87;
      --ok: #75e6a1;
      --shadow: 0 24px 80px rgba(0, 0, 0, .38);
      --radius: 8px;
      font-family: "Trebuchet MS", "Aptos", "Microsoft YaHei", sans-serif;
    }}
    .theme-light {{
      color-scheme: light;
      --bg: #eef5f1;
      --panel: rgba(255, 255, 255, .78);
      --panel-strong: rgba(255, 255, 255, .94);
      --text: #11221d;
      --muted: #526b62;
      --line: rgba(3, 92, 75, .18);
      --cyan: #008f73;
      --cyan-soft: rgba(0, 143, 115, .12);
      --amber: #a86800;
      --danger: #b3263a;
      --risk: #806000;
      --ok: #0d7b45;
      --shadow: 0 24px 70px rgba(13, 44, 38, .16);
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--bg); }}
    body {{
      min-height: 100dvh;
      margin: 0;
      color: var(--text);
      background:
        linear-gradient(120deg, rgba(72, 242, 194, .12), transparent 32%),
        radial-gradient(circle at 82% 8%, rgba(255, 191, 77, .16), transparent 28%),
        repeating-linear-gradient(90deg, rgba(255,255,255,.035) 0 1px, transparent 1px 54px),
        repeating-linear-gradient(0deg, rgba(255,255,255,.03) 0 1px, transparent 1px 54px),
        var(--bg);
      letter-spacing: 0;
      overflow-x: hidden;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image: radial-gradient(rgba(255,255,255,.18) 1px, transparent 1px);
      background-size: 3px 3px;
      opacity: .08;
      mix-blend-mode: screen;
    }}
    a {{ color: var(--cyan); text-underline-offset: 3px; }}
    .shell {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 34px 0 56px; }}
    .hero {{
      position: relative;
      display: grid;
      grid-template-columns: 1.3fr .7fr;
      gap: 22px;
      align-items: stretch;
      margin-bottom: 20px;
      animation: rise .5s ease-out both;
    }}
    .hero-main, .signal-panel, .station-card, .empty-state {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }}
    .hero-main {{ padding: 28px; overflow: hidden; position: relative; }}
    .hero-main::after {{
      content: "";
      position: absolute;
      right: -90px;
      top: -110px;
      width: 260px;
      height: 260px;
      border: 1px solid var(--line);
      border-radius: 50%;
      box-shadow: inset 0 0 48px var(--cyan-soft);
    }}
    .eyebrow {{ margin: 0 0 12px; color: var(--cyan); font-weight: 700; text-transform: uppercase; font-size: 12px; }}
    h1 {{ margin: 0; max-width: 760px; font-size: clamp(30px, 6vw, 68px); line-height: .98; letter-spacing: 0; }}
    .hero-copy {{ margin: 18px 0 0; max-width: 760px; color: var(--muted); font-size: 17px; line-height: 1.65; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }}
    .chip {{
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 9px 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,.045);
      color: var(--muted);
      font-size: 13px;
    }}
    .chip b {{ color: var(--text); font-weight: 700; }}
    .signal-panel {{
      padding: 22px;
      display: grid;
      gap: 14px;
      background: var(--panel-strong);
    }}
    .signal {{
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
    }}
    .signal:last-child {{ border-bottom: 0; padding-bottom: 0; }}
    .signal span {{ display: block; color: var(--muted); font-size: 12px; }}
    .signal strong {{ display: block; margin-top: 5px; font-size: 28px; font-variant-numeric: tabular-nums; }}
    .board {{ display: grid; gap: 16px; }}
    .station-card {{
      position: relative;
      overflow: hidden;
      padding: 20px;
      animation: cardIn .52s ease-out both;
      animation-delay: var(--delay);
      transition: transform .22s ease, border-color .22s ease, background .22s ease;
    }}
    .station-card:hover, .station-card:focus-within {{
      transform: translateY(-3px);
      border-color: color-mix(in srgb, var(--cyan) 48%, transparent);
    }}
    .station-card-prime::before {{
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(110deg, transparent 0 34%, rgba(72,242,194,.18) 48%, transparent 62%);
      transform: translateX(-100%);
      animation: scan 3.6s ease-in-out infinite;
      pointer-events: none;
    }}
    .card-orbit {{
      position: absolute;
      width: 170px;
      height: 170px;
      right: -82px;
      top: -92px;
      border: 1px solid var(--line);
      border-radius: 50%;
      opacity: .65;
    }}
    .station-head {{
      position: relative;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 14px;
    }}
    .rank-kicker {{ margin: 0 0 5px; color: var(--amber); font-size: 12px; font-weight: 800; text-transform: uppercase; }}
    h2 {{ margin: 0; font-size: clamp(20px, 3vw, 30px); letter-spacing: 0; }}
    .test-badge, .record-count {{
      display: inline-flex;
      margin-left: 10px;
      padding: 5px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--amber);
      font-size: 12px;
      vertical-align: middle;
    }}
    .record-count {{ margin-left: 0; color: var(--cyan); white-space: nowrap; }}
    .station-meta {{
      display: grid;
      grid-template-columns: minmax(0, 1.6fr) .55fr .8fr;
      gap: 10px;
      margin-bottom: 12px;
    }}
    .station-meta span {{
      min-width: 0;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      color: var(--muted);
      background: rgba(255,255,255,.035);
      overflow-wrap: anywhere;
    }}
    .station-meta b {{ display: block; margin-bottom: 4px; color: var(--text); font-size: 12px; }}
    .station-notes {{ margin: 0 0 16px; color: var(--muted); line-height: 1.65; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: var(--radius); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 900px; background: rgba(0,0,0,.12); }}
    th, td {{ padding: 13px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 800; text-transform: uppercase; background: rgba(255,255,255,.04); }}
    td {{ color: var(--text); line-height: 1.55; }}
    tr:last-child td {{ border-bottom: 0; }}
    .num, .price {{ font-family: "Cascadia Mono", "Consolas", monospace; font-variant-numeric: tabular-nums; }}
    .price {{ color: var(--cyan); font-weight: 800; }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tag {{
      display: inline-flex;
      padding: 5px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      font-size: 12px;
      line-height: 1.25;
      background: rgba(255,255,255,.045);
    }}
    .tag-ok {{ color: var(--ok); }}
    .tag-risk {{ color: var(--risk); }}
    .tag-danger {{ color: var(--danger); }}
    .empty-state {{
      min-height: 360px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 44px 20px;
      animation: rise .5s ease-out both;
    }}
    .empty-state h2 {{ margin: 16px 0 8px; }}
    .empty-state p {{ margin: 0; color: var(--muted); }}
    .empty-pulse {{ width: 82px; height: 82px; border-radius: 50%; border: 1px solid var(--cyan); box-shadow: 0 0 44px var(--cyan-soft); }}
    .empty-row {{ text-align: center; color: var(--muted); }}
    footer {{ margin-top: 18px; color: var(--muted); font-size: 12px; text-align: center; }}
    @keyframes rise {{ from {{ opacity: 0; transform: translateY(14px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    @keyframes cardIn {{ from {{ opacity: 0; transform: translateY(18px) scale(.99); }} to {{ opacity: 1; transform: translateY(0) scale(1); }} }}
    @keyframes scan {{ 0%, 48% {{ transform: translateX(-100%); }} 68%, 100% {{ transform: translateX(100%); }} }}
    @media (max-width: 860px) {{
      .shell {{ width: min(100% - 20px, 760px); padding-top: 18px; }}
      .hero {{ grid-template-columns: 1fr; }}
      .hero-main {{ padding: 22px; }}
      .station-meta {{ grid-template-columns: 1fr; }}
      .station-head {{ display: grid; }}
      .record-count {{ justify-self: start; }}
      table {{ min-width: 0; }}
      thead {{ display: none; }}
      tr {{ display: grid; gap: 8px; padding: 12px; border-bottom: 1px solid var(--line); }}
      tr:last-child {{ border-bottom: 0; }}
      td {{ display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 10px; padding: 0; border-bottom: 0; overflow-wrap: anywhere; }}
      td::before {{ content: attr(data-label); color: var(--muted); font-size: 12px; font-weight: 800; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ animation: none !important; transition: none !important; scroll-behavior: auto !important; }}
    }}
  </style>
</head>
<body class="{html_attr(theme_class)}">
  <main class="shell">
    <section class="hero">
      <div class="hero-main">
        <p class="eyebrow">Model Price Intelligence</p>
        <h1>{html_escape(title)}</h1>
        <p class="hero-copy">围绕 {html_escape(model_text)} 的站点价格信号面板，按 {html_escape(metric_text)} 聚合展示，保留可信度、过期与异常低价提醒。</p>
        <div class="chips">{filter_chips}</div>
      </div>
      <aside class="signal-panel" aria-label="排行概览">
        <div class="signal"><span>命中站点</span><strong>{card_count}</strong></div>
        <div class="signal"><span>价格记录</span><strong>{record_count}</strong></div>
        <div class="signal"><span>生成时间</span><strong>{html_escape(generated_at)}</strong></div>
      </aside>
    </section>
    <section class="board" aria-label="站点排行列表">
      {cards}
    </section>
    <footer>由 model-price-calculator 本地价格库生成 · HTML 仅用于展示，不会修改数据</footer>
  </main>
</body>
</html>"""


def maybe_write_html_output(result: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    output_file = normalize_text(payload.get("output_file"))
    if not output_file:
        output_file = str(Path(tempfile.gettempdir()) / f"model-price-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html")
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.get("html", ""), encoding="utf-8")
    result["output_file"] = str(output_path)
    return result


def dashboard_record_item(
    registry: Dict[str, Any],
    station: Dict[str, Any],
    record: Dict[str, Any],
    medians: Dict[str, Any],
) -> Dict[str, Any]:
    computed = compute(build_record_pricing_payload(station, record))
    score, _ = record_confidence(record, station)
    warnings = [
        warning
        for warning in record_health_warnings(station, record, anomaly_low_price_warnings(record, computed, medians))
        if "可信度" not in normalize_text(warning)
    ]
    cheap_reasons = item_explain_parts(station, record, "summary_rmb_per_m", computed)
    dimensions = computed.get("dimensions", {}) if isinstance(computed.get("dimensions"), dict) else {}
    return {
        "record_id": normalize_text(record.get("record_id")),
        "station_id": normalize_text(record.get("station_id")),
        "station_name": normalize_text(station.get("name") or record.get("station_id")),
        "website": normalize_text(station.get("website")),
        "invite_url": normalize_text(station.get("invite_url")),
        "notes": normalize_text(station.get("notes")),
        "recharge_ratio": resolve_station_recharge_ratio(station, record),
        "model_name": normalize_text(record.get("model_name")),
        "group": normalize_text(record.get("group")),
        "group_note": normalize_text(record.get("group_note")),
        "original_price": build_original_price_text(record),
        "multiplier": trim_decimal_text(computed.get("multiplier") or record.get("multiplier") or "1"),
        "computed_price": build_computed_price_text(record, computed),
        "summary_rmb_per_m": normalize_text(computed.get("summary", {}).get("rmb_per_m")),
        "input_rmb_per_m": normalize_text((dimensions.get("input") or {}).get("rmb_per_m")),
        "output_rmb_per_m": normalize_text((dimensions.get("output") or {}).get("rmb_per_m")),
        "cache_read_rmb_per_m": normalize_text((dimensions.get("cache_read") or {}).get("rmb_per_m")),
        "cache_write_rmb_per_m": normalize_text((dimensions.get("cache_write") or {}).get("rmb_per_m")),
        "confidence_score": score,
        "confidence_label": confidence_label(score),
        "warnings": warnings,
        "cheap_reasons": cheap_reasons,
        "last_verified_at": iso_to_display(record_verified_at(station, record)),
        "updated_at": iso_to_display(record.get("updated_at") or station.get("updated_at")),
        "station_updated_at": iso_to_display(station.get("updated_at")),
        "copy_text": computed.get("copy_text"),
    }


def build_dashboard_data(registry: Dict[str, Any]) -> Dict[str, Any]:
    stations_by_id = {
        normalize_text(station.get("station_id")): station
        for station in registry.get("stations", [])
    }
    medians = model_summary_medians(registry)
    records = []
    for record in registry.get("price_records", []):
        station = stations_by_id.get(normalize_text(record.get("station_id")), {})
        if not station:
            continue
        records.append(dashboard_record_item(registry, station, record, medians))
    models = sorted(unique_strings([record.get("model_name") for record in records]), key=lambda item: normalize_key(item))
    groups = sorted(unique_strings([record.get("group") for record in records]), key=lambda item: normalize_key(item))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "generated_at": generated_at,
        "station_count": len(registry.get("stations", [])),
        "record_count": len(records),
        "models": models,
        "groups": groups,
        "records": records,
    }


def render_dashboard_html(data: Dict[str, Any], payload: Dict[str, Any]) -> str:
    title = normalize_text(payload.get("title")) or "站点价格实时雷达"
    theme = "light" if normalize_text(payload.get("theme")).lower() == "light" else "dark"
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN" data-theme="{html_attr(theme)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #050b0a;
      --ink: #effdf8;
      --muted: #8fb2a7;
      --line: rgba(119, 255, 218, .16);
      --panel: rgba(9, 24, 22, .76);
      --panel2: rgba(15, 37, 33, .9);
      --mint: #4cf6c4;
      --gold: #ffbf4d;
      --red: #ff667a;
      --green: #78eda8;
      --shadow: 0 26px 80px rgba(0,0,0,.45);
    }}
    [data-theme="light"] {{
      color-scheme: light;
      --bg: #eef5f1;
      --ink: #10231e;
      --muted: #526a62;
      --line: rgba(0, 118, 95, .17);
      --panel: rgba(255,255,255,.78);
      --panel2: rgba(255,255,255,.94);
      --mint: #008f73;
      --gold: #a96800;
      --red: #b3263a;
      --green: #0a7f46;
      --shadow: 0 24px 70px rgba(9, 45, 36, .16);
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--bg); }}
    body {{
      min-height: 100dvh;
      margin: 0;
      color: var(--ink);
      font-family: "Trebuchet MS", "Aptos", "Microsoft YaHei", sans-serif;
      letter-spacing: 0;
      overflow-x: hidden;
      background:
        radial-gradient(circle at 16% 8%, rgba(76,246,196,.18), transparent 30%),
        radial-gradient(circle at 86% 12%, rgba(255,191,77,.16), transparent 26%),
        linear-gradient(135deg, rgba(255,255,255,.055), transparent 34%),
        repeating-linear-gradient(90deg, rgba(255,255,255,.036) 0 1px, transparent 1px 56px),
        repeating-linear-gradient(0deg, rgba(255,255,255,.03) 0 1px, transparent 1px 56px),
        var(--bg);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      background-image: radial-gradient(rgba(255,255,255,.2) 1px, transparent 1px);
      background-size: 3px 3px;
      opacity: .07;
      pointer-events: none;
    }}
    a {{ color: var(--mint); text-underline-offset: 3px; }}
    button, select, input {{
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.055);
      color: var(--ink);
      font: inherit;
    }}
    button {{ cursor: pointer; padding: 0 14px; transition: transform .18s ease, border-color .18s ease, background .18s ease; }}
    button:hover {{ transform: translateY(-1px); border-color: var(--mint); }}
    select option {{
      background: #0f251f;
      color: #effdf8;
    }}
    [data-theme="light"] select option {{
      background: #ffffff;
      color: #10231e;
    }}
    button:focus-visible, select:focus-visible, input:focus-visible, a:focus-visible {{ outline: 3px solid rgba(76,246,196,.34); outline-offset: 2px; }}
    .shell {{ width: min(1240px, calc(100% - 28px)); margin: 0 auto; padding: 30px 0 48px; }}
    .hero {{
      display: grid;
      grid-template-columns: 1.25fr .75fr;
      gap: 18px;
      margin-bottom: 18px;
      animation: rise .45s ease-out both;
    }}
    .panel, .card, .toolbar, .empty {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }}
    .headline {{ position: relative; overflow: hidden; padding: 28px; }}
    .headline::after {{
      content: "";
      position: absolute;
      width: 320px;
      height: 320px;
      right: -120px;
      top: -150px;
      border: 1px solid var(--line);
      border-radius: 50%;
      box-shadow: inset 0 0 54px rgba(76,246,196,.18);
    }}
    .eyebrow {{ margin: 0 0 12px; color: var(--mint); font-size: 12px; font-weight: 800; text-transform: uppercase; }}
    h1 {{ margin: 0; max-width: 820px; font-size: clamp(32px, 6vw, 72px); line-height: .98; letter-spacing: 0; }}
    .copy {{ margin: 18px 0 0; max-width: 780px; color: var(--muted); line-height: 1.65; font-size: 17px; }}
    .stats {{ display: grid; gap: 12px; padding: 20px; background: var(--panel2); }}
    .stat {{ border-bottom: 1px solid var(--line); padding-bottom: 12px; }}
    .stat:last-child {{ border-bottom: 0; padding-bottom: 0; }}
    .stat span {{ display: block; color: var(--muted); font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 30px; font-variant-numeric: tabular-nums; }}
    .toolbar {{
      position: sticky;
      top: 10px;
      z-index: 20;
      display: grid;
      grid-template-columns: minmax(180px, 1fr) repeat(5, minmax(112px, auto)) minmax(92px, auto);
      gap: 10px;
      align-items: center;
      padding: 12px;
      margin-bottom: 18px;
    }}
    .field {{ display: grid; gap: 6px; min-width: 0; }}
    .field label {{ color: var(--muted); font-size: 12px; font-weight: 800; }}
    .field input, .field select {{ width: 100%; padding: 0 12px; }}
    .reset-field {{ align-self: end; }}
    .reset-button {{
      width: 100%;
      color: var(--ink);
      font-weight: 800;
      background: linear-gradient(135deg, rgba(76,246,196,.12), rgba(255,191,77,.08));
    }}
    .board {{ display: grid; gap: 14px; }}
    .card {{
      position: relative;
      overflow: hidden;
      padding: 18px;
      animation: cardIn .46s ease-out both;
      animation-delay: var(--delay, 0ms);
      transition: transform .2s ease, border-color .2s ease;
    }}
    .card:hover {{ transform: translateY(-3px); border-color: color-mix(in srgb, var(--mint) 48%, transparent); }}
    .card.prime::before {{
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(110deg, transparent 0 36%, rgba(76,246,196,.18) 49%, transparent 63%);
      transform: translateX(-100%);
      animation: scan 3.4s ease-in-out infinite;
      pointer-events: none;
    }}
    .card-head {{ display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; margin-bottom: 12px; }}
    .rank {{ margin: 0 0 4px; color: var(--gold); font-size: 12px; font-weight: 900; text-transform: uppercase; }}
    h2 {{ margin: 0; font-size: clamp(20px, 3vw, 30px); letter-spacing: 0; }}
    .price {{ color: var(--mint); font-family: "Cascadia Mono", Consolas, monospace; font-weight: 900; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .detail-list {{ display: grid; gap: 8px; }}
    .detail-row {{
      display: grid;
      grid-template-columns: 118px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: rgba(0,0,0,.12);
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .detail-row b {{ color: var(--muted); font-size: 12px; }}
    .detail-row strong {{ color: var(--ink); font-weight: 600; }}
    .copy-price {{
      position: relative;
      cursor: pointer;
      transition: transform .18s ease, border-color .18s ease, background .18s ease;
    }}
    .copy-price::after {{
      content: "点击复制";
      align-self: start;
      justify-self: end;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      color: var(--mint);
      font-size: 12px;
      font-weight: 800;
      background: rgba(76,246,196,.07);
    }}
    .copy-price:hover {{ transform: translateY(-1px); border-color: color-mix(in srgb, var(--mint) 50%, transparent); background: rgba(76,246,196,.07); }}
    .copy-price:focus-visible {{ outline: 3px solid rgba(76,246,196,.34); outline-offset: 2px; }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tag {{ border: 1px solid var(--line); border-radius: 999px; padding: 5px 8px; font-size: 12px; background: rgba(255,255,255,.045); }}
    .tag.ok {{ color: var(--green); }}
    .tag.warn {{ color: var(--gold); }}
    .tag.bad {{ color: var(--red); }}
    .empty {{ min-height: 300px; display: grid; place-items: center; text-align: center; padding: 32px; }}
    .empty h2 {{ margin: 0 0 8px; }}
    .empty p {{ margin: 0; color: var(--muted); }}
    .toast {{
      position: fixed;
      right: 20px;
      bottom: 20px;
      z-index: 60;
      transform: translateY(16px);
      opacity: 0;
      pointer-events: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      color: var(--ink);
      font-weight: 900;
      background: var(--panel2);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      transition: opacity .2s ease, transform .2s ease;
    }}
    .toast.show {{ opacity: 1; transform: translateY(0); }}
    footer {{ margin-top: 16px; color: var(--muted); font-size: 12px; text-align: center; }}
    @keyframes rise {{ from {{ opacity: 0; transform: translateY(14px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    @keyframes cardIn {{ from {{ opacity: 0; transform: translateY(16px) scale(.99); }} to {{ opacity: 1; transform: translateY(0) scale(1); }} }}
    @keyframes scan {{ 0%, 46% {{ transform: translateX(-100%); }} 70%, 100% {{ transform: translateX(100%); }} }}
    @media (max-width: 980px) {{
      .hero, .toolbar {{ grid-template-columns: 1fr; }}
      .toolbar {{ position: static; }}
      .detail-row {{ grid-template-columns: 1fr; gap: 5px; }}
      .copy-price::after {{ justify-self: start; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ animation: none !important; transition: none !important; scroll-behavior: auto !important; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="panel headline">
        <p class="eyebrow">Cached Price Dashboard</p>
        <h1>{html_escape(title)}</h1>
        <p class="copy">这个页面是预生成缓存仪表盘。打开时不再调用 Python，模型、分组、排序和 TopN 都在浏览器本地完成。</p>
      </div>
      <aside class="panel stats">
        <div class="stat"><span>站点数</span><strong id="stationCount">0</strong></div>
        <div class="stat"><span>价格记录</span><strong id="recordCount">0</strong></div>
        <div class="stat"><span>生成时间</span><strong id="generatedAt">-</strong></div>
      </aside>
    </section>
    <section class="toolbar" aria-label="筛选工具栏">
      <div class="field"><label for="search">搜索</label><input id="search" type="search" placeholder="站点、官网、备注、分组"></div>
      <div class="field"><label for="model">模型</label><select id="model"></select></div>
      <div class="field"><label for="group">分组</label><select id="group"></select></div>
      <div class="field"><label for="metric">排序</label><select id="metric"></select></div>
      <div class="field"><label for="viewMode">视图</label><select id="viewMode"><option value="station" selected>按站点</option><option value="record">按记录</option></select></div>
      <div class="field"><label for="limit">TopN</label><select id="limit"><option>5</option><option selected>10</option><option>20</option><option>50</option><option value="9999">全部</option></select></div>
      <div class="field reset-field"><button id="resetFilters" class="reset-button" type="button">重置</button></div>
    </section>
    <section id="board" class="board" aria-live="polite"></section>
    <div id="toast" class="toast" role="status" aria-live="polite">已复制</div>
    <footer>本页由 model-price-calculator 预生成 · 修改价格库后请刷新缓存页面</footer>
  </main>
  <script id="dashboard-data" type="application/json">{data_json}</script>
  <script>
    const dashboard = JSON.parse(document.getElementById('dashboard-data').textContent);
    const state = {{ search: '', model: '', group: '', metric: 'summary_rmb_per_m', viewMode: 'station', limit: 10 }};
    const metricLabels = {{
      summary_rmb_per_m: '综合价',
      input_rmb_per_m: '输入价',
      output_rmb_per_m: '输出价',
      cache_read_rmb_per_m: '缓存读取价'
    }};
    const byId = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
    const numberValue = (value) => {{
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
    }};
    const safeLink = (url) => /^https?:\\/\\//i.test(url || '') ? `<a href="${{escapeHtml(url)}}" target="_blank" rel="noopener noreferrer">${{escapeHtml(url)}}</a>` : escapeHtml(url || '未记录');
    function fillSelect(id, values, allLabel) {{
      const select = byId(id);
      select.innerHTML = `<option value="">${{allLabel}}</option>` + values.map((item) => `<option value="${{escapeHtml(item)}}">${{escapeHtml(item)}}</option>`).join('');
    }}
    function tagHtml(text) {{
      const klass = /低|过期|异常|未记录/.test(text) ? 'bad' : 'warn';
      return `<span class="tag ${{klass}}">${{escapeHtml(text)}}</span>`;
    }}
    let toastTimer = null;
    function showToast(message) {{
      const toast = byId('toast');
      toast.textContent = message;
      toast.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove('show'), 1400);
    }}
    function legacyCopyText(value) {{
      const textarea = document.createElement('textarea');
      textarea.value = value;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      textarea.style.top = '0';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      textarea.setSelectionRange(0, textarea.value.length);
      let copied = false;
      try {{
        copied = document.execCommand('copy');
      }} finally {{
        textarea.remove();
      }}
      return copied;
    }}
    async function copyText(text) {{
      const value = String(text || '').trim();
      if (!value) return;
      let copied = false;
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          await navigator.clipboard.writeText(value);
          copied = true;
        }}
      }} catch (error) {{
        copied = false;
      }}
      if (!copied) {{
        copied = legacyCopyText(value);
      }}
      if (copied) {{
        showToast('已复制');
      }} else {{
        showToast('复制失败，请手动选中复制');
      }}
    }}
    function copyPriceRow(record) {{
      const text = record.copy_text || record.original_price || '';
      return `<div class="detail-row copy-price" role="button" tabindex="0" data-copy="${{escapeHtml(text)}}" title="点击复制文案"><b>原始价格</b><strong>${{escapeHtml(record.original_price || '-')}}</strong></div>`;
    }}
    function representativeRecords(records) {{
      if (state.viewMode === 'record') return records;
      const grouped = new Map();
      for (const record of records) {{
        const stationRecords = grouped.get(record.station_id) || [];
        stationRecords.push(record);
        grouped.set(record.station_id, stationRecords);
      }}
      return Array.from(grouped.values()).map((stationRecords) => {{
        const representative = stationRecords.reduce((best, record) => {{
          const bestValue = numberValue(best[state.metric]);
          const recordValue = numberValue(record[state.metric]);
          return recordValue < bestValue ? record : best;
        }}, stationRecords[0]);
        return {{ ...representative, station_record_count: stationRecords.length }};
      }});
    }}
    function render() {{
      const query = state.search.trim().toLowerCase();
      let records = dashboard.records.filter((record) => {{
        if (state.model && record.model_name !== state.model) return false;
        if (state.group && record.group !== state.group) return false;
        if (!query) return true;
        return [record.station_name, record.website, record.invite_url, record.notes, record.model_name, record.group, record.group_note]
          .some((value) => String(value || '').toLowerCase().includes(query));
      }});
      records = representativeRecords(records);
      records.sort((left, right) => {{
        return numberValue(left[state.metric]) - numberValue(right[state.metric]);
      }});
      records = records.slice(0, Number(state.limit));
      const board = byId('board');
      if (!records.length) {{
        board.innerHTML = '<section class="empty"><div><h2>暂无命中记录</h2><p>换一个模型、分组或搜索词试试。</p></div></section>';
        return;
      }}
      board.innerHTML = records.map((record, index) => {{
        const warningItems = record.warnings || [];
        const warnings = warningItems.map(tagHtml).join('');
        const warningRow = warningItems.length ? `<div class="detail-row"><b>提醒</b><div class="tags">${{warnings}}</div></div>` : '';
        return `<article class="card ${{index === 0 ? 'prime' : ''}}" style="--delay:${{Math.min(index * 35, 420)}}ms">
          <header class="card-head">
            <div><p class="rank">${{index === 0 ? 'Prime' : `No.${{index + 1}}`}}</p><h2>${{escapeHtml(record.station_name)}}${{state.viewMode === 'station' ? ` · ${{record.station_record_count || 1}} 条命中` : ''}}</h2></div>
            <div class="price">${{escapeHtml(record[state.metric] || record.summary_rmb_per_m || '-')}}/M</div>
          </header>
          <div class="detail-list">
            <div class="detail-row"><b>官网</b><strong>${{safeLink(record.website)}}</strong></div>
            <div class="detail-row"><b>邀请链接</b><strong>${{safeLink(record.invite_url)}}</strong></div>
            <div class="detail-row"><b>站点备注</b><strong>${{escapeHtml(record.notes || '无备注')}}</strong></div>
            <div class="detail-row"><b>模型/分组</b><strong>${{escapeHtml(record.model_name)}} / ${{escapeHtml(record.group)}}</strong></div>
            <div class="detail-row"><b>分组备注</b><strong>${{escapeHtml(record.group_note || '-')}}</strong></div>
            <div class="detail-row"><b>充值比</b><strong>${{escapeHtml(record.recharge_ratio || '1:1')}}</strong></div>
            <div class="detail-row"><b>倍率</b><strong>${{escapeHtml(record.multiplier)}}</strong></div>
            ${{copyPriceRow(record)}}
            <div class="detail-row"><b>折算价格</b><strong>${{escapeHtml(record.computed_price)}}</strong></div>
            ${{warningRow}}
            <div class="detail-row"><b>更新时间</b><strong>${{escapeHtml(record.updated_at || record.station_updated_at || '-')}}</strong></div>
          </div>
        </article>`;
      }}).join('');
      board.querySelectorAll('[data-copy]').forEach((item) => {{
        item.addEventListener('click', () => copyText(item.dataset.copy));
        item.addEventListener('keydown', (event) => {{
          if (event.key === 'Enter' || event.key === ' ') {{
            event.preventDefault();
            copyText(item.dataset.copy);
          }}
        }});
      }});
    }}
    function bind() {{
      byId('stationCount').textContent = dashboard.station_count;
      byId('recordCount').textContent = dashboard.record_count;
      byId('generatedAt').textContent = dashboard.generated_at;
      fillSelect('model', dashboard.models, '全部模型');
      fillSelect('group', dashboard.groups, '全部分组');
      byId('metric').innerHTML = Object.entries(metricLabels).map(([value, label]) => `<option value="${{value}}">${{label}}</option>`).join('');
      ['model', 'group', 'metric', 'viewMode', 'limit'].forEach((id) => byId(id).addEventListener('change', (event) => {{ state[id] = event.target.value; render(); }}));
      byId('search').addEventListener('input', (event) => {{ state.search = event.target.value; render(); }});
      byId('resetFilters').addEventListener('click', () => {{
        state.search = '';
        state.model = '';
        state.group = '';
        state.metric = 'summary_rmb_per_m';
        state.viewMode = 'station';
        state.limit = 10;
        byId('search').value = '';
        byId('model').value = '';
        byId('group').value = '';
        byId('metric').value = state.metric;
        byId('viewMode').value = state.viewMode;
        byId('limit').value = String(state.limit);
        render();
      }});
      render();
    }}
    bind();
  </script>
</body>
</html>"""


def write_dashboard_files(registry: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    html_path = Path(normalize_text(payload.get("output_file")) or DASHBOARD_HTML_PATH)
    meta_path = html_path.with_suffix(".meta.json") if html_path != DASHBOARD_HTML_PATH else DASHBOARD_META_PATH
    data = build_dashboard_data(registry)
    html_text = render_dashboard_html(data, payload)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_text, encoding="utf-8")
    meta = {
        "generated_at": data.get("generated_at"),
        "html_file": str(html_path),
        "station_count": data.get("station_count"),
        "record_count": data.get("record_count"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "html_file": str(html_path),
        "meta_file": str(meta_path),
        "generated_at": data.get("generated_at"),
        "station_count": data.get("station_count"),
        "record_count": data.get("record_count"),
    }


def dashboard_html_status(registry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    html_path = Path(normalize_text(payload.get("output_file")) or DASHBOARD_HTML_PATH)
    if not html_path.exists() or normalize_bool(payload.get("refresh")):
        dashboard = write_dashboard_files(registry, payload)
        dashboard["refreshed"] = True
        return dashboard
    result = {
        "html_file": str(html_path),
        "exists": True,
        "refreshed": False,
    }
    meta_path = html_path.with_suffix(".meta.json") if html_path != DASHBOARD_HTML_PATH else DASHBOARD_META_PATH
    if meta_path.exists():
        try:
            result["meta"] = load_json_file(str(meta_path))
        except Exception:
            result["meta_error"] = "缓存元数据无法读取"
    return result


def refresh_dashboard_after_write(registry: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    if result.pop("_skip_dashboard_refresh", False):
        return result
    if result.get("needs_confirmation"):
        return result
    try:
        result["dashboard"] = write_dashboard_files(registry, {})
    except Exception as error:
        result["dashboard_error"] = str(error)
    return result


def build_rank_station_html(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    rank_result, views, station_ids = collect_rank_station_html_views(registry, query)
    html_text = render_station_html_document(views, query, rank_result.get("filters", {}), "rank")
    result = {
        "count": len(views),
        "station_ids": station_ids,
        "filters": rank_result.get("filters", {}),
        "html": html_text,
    }
    return maybe_write_html_output(result, query)


def build_station_html(registry: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    views = collect_station_html_views(registry, query)
    filters = {
        "model_name": query.get("model_name") or None,
        "group": query.get("group") or None,
        "metric": query.get("sort_by") or query.get("metric") or "summary_rmb_per_m",
        "direction": normalize_text(query.get("direction") or "asc").lower(),
        "include_terms": ensure_list(query.get("include_terms") or query.get("include") or query.get("包含")),
        "exclude_terms": ensure_list(query.get("exclude_terms") or query.get("exclude") or query.get("排除")),
        "min_confidence": normalize_confidence(query.get("min_confidence") or query.get("最低可信度")),
    }
    html_text = render_station_html_document(views, query, filters, "stations")
    result = {
        "count": len(views),
        "html": html_text,
    }
    return maybe_write_html_output(result, query)


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
            "rank-stations-html",
            "dashboard-html",
            "refresh-dashboard-html",
            "list",
            "leaderboard",
            "update-station",
            "patch-record",
            "delete-records",
            "history",
            "stations-md",
            "stations-html",
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
    elif args.command == "rank-stations-html":
        result = build_rank_station_html(registry, payload)
    elif args.command == "dashboard-html":
        result = dashboard_html_status(registry, payload)
    elif args.command == "refresh-dashboard-html":
        result = write_dashboard_files(registry, payload)
    elif args.command == "leaderboard":
        result = build_leaderboard_copy(rank_records(registry, payload))
    elif args.command == "update-station":
        result = update_station_fields(registry, payload)
    elif args.command == "patch-record":
        result = patch_record_fields(registry, payload)
    elif args.command == "delete-records":
        result = delete_records(registry, payload)
    elif args.command == "history":
        result = query_price_history(registry, payload)
    elif args.command == "stations-md":
        result = build_station_markdown(registry, payload)
    elif args.command == "stations-html":
        result = build_station_html(registry, payload)
    elif args.command == "cleanup-test":
        result = cleanup_test_stations(registry)
    else:
        result = list_registry(registry, payload)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
