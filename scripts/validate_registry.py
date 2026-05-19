#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from calc_model_price import compute, parse_ratio, to_decimal
from model_catalog import canonical_model_name
from site_price_registry import (
    build_record_pricing_payload,
    normalize_confidence,
    normalize_key,
    normalize_text,
    parse_iso_datetime,
)


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "assets" / "site-price-registry.json"
HISTORY_PATH = Path(__file__).resolve().parent.parent / "assets" / "site-price-history.json"


def load_json_file(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


def issue(level: str, code: str, message: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "message": message,
        "context": context or {},
    }


def comparable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: comparable(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [comparable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def computed_equal(left: Any, right: Any) -> bool:
    return comparable(left) == comparable(right)


def records_by_station(registry: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in registry.get("price_records", []):
        grouped.setdefault(normalize_text(record.get("station_id")), []).append(record)
    return grouped


def validate_top_level(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues = []
    if "version" not in registry:
        issues.append(issue("error", "missing_version", "价格库缺少 version 字段"))
    if not isinstance(registry.get("stations"), list):
        issues.append(issue("error", "invalid_stations", "stations 必须是数组"))
    if not isinstance(registry.get("price_records"), list):
        issues.append(issue("error", "invalid_price_records", "price_records 必须是数组"))
    return issues


def validate_stations(registry: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    issues = []
    stations_by_id: Dict[str, Dict[str, Any]] = {}
    seen = set()
    station_signatures: Dict[str, str] = {}

    for index, station in enumerate(registry.get("stations", [])):
        if not isinstance(station, dict):
            issues.append(issue("error", "invalid_station", "站点记录必须是对象", {"index": index}))
            continue

        station_id = normalize_text(station.get("station_id"))
        if not station_id:
            issues.append(issue("error", "missing_station_id", "站点缺少 station_id", {"index": index}))
            continue
        if station_id in seen:
            issues.append(issue("error", "duplicate_station_id", "站点 ID 重复", {"station_id": station_id}))
        seen.add(station_id)
        stations_by_id[station_id] = station

        if not normalize_text(station.get("name")):
            issues.append(issue("warning", "missing_station_name", "站点缺少名称", {"station_id": station_id}))
        if not normalize_text(station.get("website")):
            issues.append(issue("warning", "missing_website", "站点缺少官网", {"station_id": station_id}))

        ratio = normalize_text(station.get("recharge_ratio"))
        if ratio:
            try:
                parse_ratio(ratio)
            except ValueError as exc:
                issues.append(issue("error", "invalid_station_recharge_ratio", str(exc), {"station_id": station_id}))

        for field in ("last_verified_at", "expires_at"):
            value = normalize_text(station.get(field))
            if value and parse_iso_datetime(value) is None:
                issues.append(
                    issue(
                        "warning",
                        "invalid_station_datetime",
                        f"站点时间字段无法解析: {field}",
                        {"station_id": station_id, "field": field, "value": value},
                    )
                )
        stale_after_days = station.get("stale_after_days")
        if stale_after_days not in (None, ""):
            try:
                if int(stale_after_days) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                issues.append(
                    issue(
                        "warning",
                        "invalid_station_stale_after_days",
                        "站点 stale_after_days 必须是正整数",
                        {"station_id": station_id, "stale_after_days": stale_after_days},
                    )
                )

        signature_values = [
            normalize_key(station.get("name")),
            normalize_key(station.get("website")),
            normalize_key(station.get("api_base_url") or station.get("api_url")),
        ]
        for signature in signature_values:
            if not signature:
                continue
            if signature in station_signatures and station_signatures[signature] != station_id:
                issues.append(
                    issue(
                        "warning",
                        "possible_duplicate_station",
                        "站点名称、官网或 API 疑似重复",
                        {
                            "station_id": station_id,
                            "other_station_id": station_signatures[signature],
                            "signature": signature,
                        },
                    )
                )
            station_signatures.setdefault(signature, station_id)

    return issues, stations_by_id


def validate_price_value(record: Dict[str, Any], field: str, issues: List[Dict[str, Any]]) -> None:
    value = record.get(field)
    if value in (None, ""):
        return
    if to_decimal(value) is None:
        issues.append(
            issue(
                "error",
                "invalid_price",
                f"价格字段无法解析: {field}",
                {
                    "record_id": record.get("record_id"),
                    "station_id": record.get("station_id"),
                    "field": field,
                    "value": value,
                },
            )
        )


def validate_optional_date(record: Dict[str, Any], field: str, issues: List[Dict[str, Any]]) -> None:
    value = normalize_text(record.get(field))
    if value and parse_iso_datetime(value) is None:
        issues.append(
            issue(
                "warning",
                "invalid_datetime",
                f"时间字段无法解析: {field}",
                {"record_id": record.get("record_id"), "field": field, "value": value},
            )
        )


def validate_records(registry: Dict[str, Any], stations_by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues = []
    seen_identity = set()
    seen_record_ids = set()

    for index, record in enumerate(registry.get("price_records", [])):
        if not isinstance(record, dict):
            issues.append(issue("error", "invalid_price_record", "价格记录必须是对象", {"index": index}))
            continue

        record_id = normalize_text(record.get("record_id"))
        station_id = normalize_text(record.get("station_id"))
        model_name = normalize_text(record.get("model_name"))
        group = normalize_text(record.get("group"))

        if record_id:
            if record_id in seen_record_ids:
                issues.append(issue("error", "duplicate_record_id", "价格记录 ID 重复", {"record_id": record_id}))
            seen_record_ids.add(record_id)

        if not station_id or station_id not in stations_by_id:
            issues.append(
                issue(
                    "error",
                    "missing_station_reference",
                    "价格记录引用了不存在的站点",
                    {"record_id": record_id, "station_id": station_id},
                )
            )
            continue

        identity = (station_id, normalize_key(model_name), normalize_key(group))
        if identity in seen_identity:
            issues.append(
                issue(
                    "error",
                    "duplicate_station_model_group",
                    "同站点同模型同分组重复",
                    {"record_id": record_id, "station_id": station_id, "model_name": model_name, "group": group},
                )
            )
        seen_identity.add(identity)

        if not canonical_model_name(model_name):
            issues.append(
                issue(
                    "warning",
                    "unknown_model",
                    "模型未收录在 model-catalog.json",
                    {"record_id": record_id, "model_name": model_name},
                )
            )

        for field in ("input_price", "output_price", "cache_price", "cache_read_price", "cache_write_price"):
            validate_price_value(record, field, issues)

        confidence = record.get("confidence_score")
        if confidence not in (None, "") and normalize_confidence(confidence) is None:
            issues.append(
                issue(
                    "warning",
                    "invalid_confidence_score",
                    "confidence_score 必须是 0-1 小数或 0-100 百分数",
                    {"record_id": record_id, "confidence_score": confidence},
                )
            )

        for field in ("last_verified_at", "expires_at"):
            validate_optional_date(record, field, issues)

        stale_after_days = record.get("stale_after_days")
        if stale_after_days not in (None, ""):
            try:
                if int(stale_after_days) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                issues.append(
                    issue(
                        "warning",
                        "invalid_stale_after_days",
                        "stale_after_days 必须是正整数",
                        {"record_id": record_id, "stale_after_days": stale_after_days},
                    )
                )

        station = stations_by_id[station_id]
        ratio = normalize_text(record.get("recharge_ratio") or station.get("recharge_ratio") or "1:1")
        try:
            parse_ratio(ratio)
        except ValueError as exc:
            issues.append(issue("error", "invalid_record_recharge_ratio", str(exc), {"record_id": record_id}))

        try:
            recomputed = compute(build_record_pricing_payload(station, record))
        except Exception as exc:
            issues.append(
                issue(
                    "error",
                    "recompute_failed",
                    f"价格记录无法重算: {exc}",
                    {"record_id": record_id, "station_id": station_id},
                )
            )
            continue

        stored_computed = record.get("computed")
        if stored_computed and not computed_equal(stored_computed, recomputed):
            issues.append(
                issue(
                    "warning",
                    "computed_stale",
                    "computed 与当前算法重算结果不一致",
                    {"record_id": record_id, "station_id": station_id},
                )
            )

        summary = to_decimal(recomputed.get("summary", {}).get("rmb_per_m"))
        if summary is not None:
            if summary <= 0:
                issues.append(issue("error", "non_positive_summary_price", "综合价小于或等于 0", {"record_id": record_id}))
            elif summary < Decimal("0.01"):
                issues.append(issue("warning", "suspicious_low_summary_price", "综合价异常低", {"record_id": record_id, "summary": str(summary)}))
            elif summary > Decimal("1000"):
                issues.append(issue("warning", "suspicious_high_summary_price", "综合价异常高", {"record_id": record_id, "summary": str(summary)}))

    return issues


def validate_history_file() -> List[Dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return [issue("warning", "missing_history_file", "缺少价格变更历史文件 assets/site-price-history.json")]
    try:
        history = load_json_file(HISTORY_PATH)
    except Exception as exc:
        return [issue("warning", "invalid_history_file", f"价格变更历史文件无法读取: {exc}")]
    if not isinstance(history.get("changes"), list):
        return [issue("warning", "invalid_history_changes", "site-price-history.json 的 changes 必须是数组")]
    issues = []
    for index, change in enumerate(history.get("changes", [])):
        if not isinstance(change, dict):
            issues.append(issue("warning", "invalid_history_item", "历史记录必须是对象", {"index": index}))
            continue
        if not normalize_text(change.get("record_id")):
            issues.append(issue("warning", "missing_history_record_id", "历史记录缺少 record_id", {"index": index}))
        if parse_iso_datetime(change.get("changed_at")) is None:
            issues.append(issue("warning", "invalid_history_changed_at", "历史记录 changed_at 无法解析", {"index": index}))
    return issues


def validate_registry(registry: Dict[str, Any]) -> Dict[str, Any]:
    issues = []
    issues.extend(validate_top_level(registry))
    station_issues, stations_by_id = validate_stations(registry)
    issues.extend(station_issues)
    issues.extend(validate_records(registry, stations_by_id))
    issues.extend(validate_history_file())

    counts = {
        "error": sum(1 for item in issues if item.get("level") == "error"),
        "warning": sum(1 for item in issues if item.get("level") == "warning"),
        "info": sum(1 for item in issues if item.get("level") == "info"),
    }
    return {
        "ok": counts["error"] == 0,
        "counts": counts,
        "station_count": len(registry.get("stations", [])) if isinstance(registry.get("stations"), list) else 0,
        "record_count": len(registry.get("price_records", [])) if isinstance(registry.get("price_records"), list) else 0,
        "issues": issues,
    }


def to_markdown(result: Dict[str, Any]) -> str:
    lines = [
        "# 价格库校验报告",
        "",
        f"- 结果：{'通过' if result.get('ok') else '未通过'}",
        f"- 站点数：{result.get('station_count')}",
        f"- 价格记录数：{result.get('record_count')}",
        f"- Error：{result.get('counts', {}).get('error', 0)}",
        f"- Warning：{result.get('counts', {}).get('warning', 0)}",
        f"- Info：{result.get('counts', {}).get('info', 0)}",
        "",
    ]
    issues = result.get("issues") or []
    if not issues:
        lines.append("未发现问题。")
        return "\n".join(lines)

    lines.extend(["| 级别 | 代码 | 说明 | 上下文 |", "|---|---|---|---|"])
    for item in issues:
        context = json.dumps(item.get("context") or {}, ensure_ascii=False)
        lines.append(f"| {item.get('level')} | {item.get('code')} | {item.get('message')} | `{context}` |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate site price registry without modifying data.")
    parser.add_argument("--json-file", default=str(REGISTRY_PATH), help="Path to registry JSON")
    parser.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format")
    args = parser.parse_args()

    registry = load_json_file(Path(args.json_file))
    result = validate_registry(registry)
    if args.format == "markdown":
        print(to_markdown(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
