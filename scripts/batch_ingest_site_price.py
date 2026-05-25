#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Tuple

from registry_write_lock import registry_write_lock
from site_price_registry import build_write_summary, load_registry, upsert_record


SECTION_KEYWORDS = {
    "站点名称",
    "官网",
    "邀请链接",
    "是否已检测",
    "检测时间",
    "倍率",
    "备注",
    "充值比",
    "分组备注",
    "项目类型",
    "余额 Base URL",
    "Access Token",
    "User ID",
    "启用状态",
    "API 名称",
    "API Base URL",
    "API Key",
    "标准模型名",
    "请求模型名",
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def to_decimal(value: Any) -> Decimal:
    text = normalize_text(value)
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        raise ValueError(f"无法解析数值: {value}")
    return Decimal(match.group(0))


def format_price_like(original: str, numeric: Decimal) -> str:
    text = normalize_text(original)
    prefix = "$" if "$" in text else ("¥" if "¥" in text or "￥" in text else "")
    suffix = "/M" if "/M" in text.upper() else ("/1M Tokens" if "1M" in text.upper() else "")
    if numeric == numeric.to_integral():
        rendered = str(numeric.quantize(Decimal("1")))
    else:
        rendered = format(numeric.normalize(), "f").rstrip("0").rstrip(".")
    return f"{prefix}{rendered}{suffix}"


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig") as file:
        return file.read()


def extract_labeled_value(text: str, labels: List[str]) -> str:
    for label in labels:
        pattern = re.compile(rf"(?mi)^\s*{re.escape(label)}[:：]\s*(.+)$")
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return ""


def parse_station_info(text: str) -> Dict[str, Any]:
    station: Dict[str, Any] = {}
    field_aliases = {
        "name": ["站点名称"],
        "website": ["官网"],
        "invite_url": ["邀请链接", "邀请地址"],
        "is_checked": ["是否已检测", "是否检测", "已检测"],
        "checked_at": ["检测时间", "检查时间"],
        "notes": ["备注", "站点备注"],
        "provider_type": ["项目类型"],
        "balance_base_url": ["余额 Base URL"],
        "access_token": ["Access Token"],
        "user_id": ["User ID"],
        "probe_api_name": ["API 名称"],
        "probe_api_base_url": ["API Base URL"],
        "probe_api_key": ["API Key"],
        "canonical_model_name": ["标准模型名"],
        "request_model_name": ["请求模型名"],
    }
    for key, labels in field_aliases.items():
        value = extract_labeled_value(text, labels)
        if value:
            station[key] = value

    checked_flag = normalize_text(station.get("is_checked")).lower()
    if checked_flag in {"1", "true", "yes", "y", "已检测", "是"}:
        station["is_checked"] = True
    elif checked_flag in {"0", "false", "no", "n", "未检测", "否"}:
        station["is_checked"] = False
    if station.get("name"):
        station["alias"] = station["name"]
    return station


def parse_group_note(text: str) -> str:
    match = re.search(r"分组备注[:：]\s*(.+)", text, re.I)
    if not match:
        return ""
    return match.group(1).strip()


def locate_group_config_line(text: str) -> str:
    candidate = extract_labeled_value(text, ["分组/倍率/备注", "分组倍率备注", "倍率", "分组"])
    return candidate


def extract_group_config_block(text: str) -> str:
    pattern = re.compile(r"(?mi)^[ \t]*(分组/倍率/备注|分组倍率备注|倍率|分组)[:：][ \t]*(.*)$")
    match = pattern.search(text)
    if not match:
        return ""

    lines = []
    first_value = match.group(2).strip()
    if first_value:
        lines.append(first_value)

    for line in text[match.end() :].splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if re.match(r"^\s*[^:：]+[:：]", stripped):
            break
        if re.match(r"^gpt-[A-Za-z0-9.\-]+(?:\s+.*)?$", stripped, re.I):
            break
        lines.append(stripped)

    return "\n".join(lines).strip()


def parse_group_configs(text: str) -> Dict[str, Dict[str, Any]]:
    block = extract_group_config_block(text) or locate_group_config_line(text)
    if not block:
        return {}

    results: Dict[str, Dict[str, Any]] = {}
    pattern = re.compile(
        r"([A-Za-z0-9_\-\u4e00-\u9fa5]+)(?:\s*分组)?\s+([0-9]+(?:\.[0-9]+)?)\s*倍?(?:\(([^()]*)\))?",
        re.I,
    )
    for group, value, note in pattern.findall(block):
        results[group.lower()] = {
            "multiplier": float(value),
            "group_note": normalize_text(note),
        }

    if not results:
        numeric = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*倍?\s*", block)
        if numeric:
            results["default"] = {
                "multiplier": float(numeric.group(1)),
                "group_note": "",
            }
    return results


def parse_group_multipliers(text: str) -> Dict[str, float]:
    return {
        group: config.get("multiplier", 1.0)
        for group, config in parse_group_configs(text).items()
    }


def parse_group_notes(text: str) -> Dict[str, str]:
    configs = parse_group_configs(text)
    if configs:
        return {
            group: normalize_text(config.get("group_note"))
            for group, config in configs.items()
            if normalize_text(config.get("group_note"))
        }

    note = parse_group_note(text)
    scoped_group = detect_declared_group_scope(text)
    if note and scoped_group:
        return {scoped_group: note}
    return {}


def parse_recharge_ratio(text: str) -> str:
    raw_value = extract_labeled_value(text, ["充值比"])
    if not raw_value:
        return "1:1"
    match = re.search(r"([0-9.]+\s*:\s*[0-9.]+)", raw_value, re.I)
    if match:
        return match.group(1).replace(" ", "")
    numeric = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw_value)
    if not numeric:
        return "1:1"
    value = numeric.group(1)
    return f"1:{value}"


def is_post_multiplier_pricing(text: str) -> bool:
    keywords = [
        "倍率后的价格",
        "计算倍率后的",
        "计算倍率后的价格",
        "计算倍率后的价格信息",
        "以下都是倍率后的价格",
        "下面都是倍率后的价格",
        "以下价格都是倍率后的",
    ]
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def detect_declared_group_scope(text: str) -> str:
    patterns = [
        r"以下都是\s*([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*分组下.*?价格",
        r"以下为\s*([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*分组下.*?价格",
        r"下面都是\s*([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*分组下.*?价格",
        r"下面为\s*([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*分组下.*?价格",
        r"^\s*([A-Za-z0-9_\-\u4e00-\u9fa5]+)\s*分组下.*?价格\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip().lower()
    return ""


def parse_price_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    patterns = {
        "input_price": r"输入(?:价格)?\s*([$¥￥]?\s*[0-9.]+(?:\s*/\s*(?:1M\s*Tokens|M))?)",
        "output_price": r"(?:补全价格|输出(?:价格)?)\s*([$¥￥]?\s*[0-9.]+(?:\s*/\s*(?:1M\s*Tokens|M))?)",
        "cache_read_price": r"缓存读取(?:价格)?\s*([$¥￥]?\s*[0-9.]+(?:\s*/\s*(?:1M\s*Tokens|M))?)",
        "cache_write_price": r"缓存创建(?:价格)?\s*([$¥￥]?\s*[0-9.]+(?:\s*/\s*(?:1M\s*Tokens|M))?)",
        "cache_price": r"缓存(?:价格)?\s*([$¥￥]?\s*[0-9.]+(?:\s*/\s*(?:1M\s*Tokens|M))?)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            fields[key] = re.sub(r"\s+", "", match.group(1))
    return fields


def merge_non_empty(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def split_model_sections(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    lines = text.splitlines(keepends=True)
    offset = 0
    boundaries: List[Dict[str, Any]] = []
    header_pattern = re.compile(r"^(gpt-[A-Za-z0-9.\-]+)(?:\s+(.*))?$", re.I)
    active_group_scope = ""

    for line in lines:
        stripped = line.strip()
        declared_group_scope = detect_declared_group_scope(stripped)
        if declared_group_scope:
            active_group_scope = declared_group_scope
            offset += len(line)
            continue
        header_match = header_pattern.fullmatch(stripped) if stripped and stripped not in SECTION_KEYWORDS else None
        if header_match:
            inline_body = normalize_text(header_match.group(2))
            boundaries.append(
                {
                    "model_name": header_match.group(1),
                    "start": offset,
                    "end": offset + len(line),
                    "scoped_group": active_group_scope,
                    "inline_body": inline_body,
                }
            )
        offset += len(line)

    if not boundaries:
        return text, []

    preamble = text[: boundaries[0]["start"]]
    sections: List[Dict[str, Any]] = []
    for index, boundary in enumerate(boundaries):
        body_start = boundary["end"]
        body_end = boundaries[index + 1]["start"] if index + 1 < len(boundaries) else len(text)
        body = text[body_start:body_end].strip()
        inline_body = normalize_text(boundary.get("inline_body"))
        if inline_body:
            body = f"{inline_body}\n{body}".strip()
        sections.append(
            {
                "model_name": boundary["model_name"],
                "body": body,
                "scoped_group": boundary.get("scoped_group", ""),
            }
        )
    return preamble, sections


def parse_model_blocks(text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    preamble, sections = split_model_sections(text)
    global_context = {
        "group_multipliers": parse_group_multipliers(preamble),
        "group_notes": parse_group_notes(preamble),
        "recharge_ratio": parse_recharge_ratio(text),
        "post_multiplier_pricing": is_post_multiplier_pricing(preamble),
        "scoped_group": detect_declared_group_scope(preamble),
        "group_note": parse_group_note(preamble),
        "price_fields": parse_price_fields(preamble),
    }

    blocks = []
    for section in sections:
        body = section["body"]
        price_fields = merge_non_empty(global_context["price_fields"], parse_price_fields(body))
        model_data: Dict[str, Any] = {
            "model_name": section["model_name"],
            "group_multipliers": parse_group_multipliers(body) or global_context["group_multipliers"],
            "group_notes": parse_group_notes(body) or global_context["group_notes"],
            "recharge_ratio": parse_recharge_ratio(body) if re.search(r"充值比[:：]", body, re.I) else global_context["recharge_ratio"],
            "post_multiplier_pricing": is_post_multiplier_pricing(body) or global_context["post_multiplier_pricing"],
            "scoped_group": detect_declared_group_scope(body) or section.get("scoped_group") or global_context["scoped_group"],
            "group_note": parse_group_note(body) or global_context["group_note"],
        }
        model_data.update(price_fields)
        if model_data.get("model_name") and (
            any(model_data.get(key) for key in ("input_price", "output_price", "cache_read_price", "cache_write_price", "cache_price"))
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", normalize_text(model_data.get("model_name")))
        ):
            blocks.append(model_data)

    return global_context, blocks


def revert_post_multiplier_price(price_text: str, multiplier: float) -> str:
    if multiplier in (0, 0.0):
        return price_text
    numeric = to_decimal(price_text)
    reverted = numeric / Decimal(str(multiplier))
    return format_price_like(price_text, reverted)


def resolve_source_group(block: Dict[str, Any], groups: Dict[str, float]) -> str:
    scoped_group = normalize_text(block.get("scoped_group")).lower()
    if scoped_group:
        return scoped_group
    if block.get("post_multiplier_pricing") and len(groups) == 1:
        return next(iter(groups))
    return ""


def build_batch_payload(text: str) -> Dict[str, Any]:
    global_context, model_blocks = parse_model_blocks(text)
    preamble, _ = split_model_sections(text)
    station = parse_station_info(preamble or text)
    station["group_multipliers"] = global_context.get("group_multipliers") or {}
    station["recharge_ratio"] = global_context.get("recharge_ratio") or "1:1"

    entries = []
    for model in model_blocks:
        groups = model.get("group_multipliers") or station.get("group_multipliers") or {"default": 1.0}
        group_notes = model.get("group_notes") or global_context.get("group_notes") or {}
        source_group = resolve_source_group(model, groups)
        target_groups = groups
        if source_group:
            target_groups = {source_group: groups.get(source_group, 1.0)}

        base_prices = {
            key: model.get(key)
            for key in ("input_price", "output_price", "cache_read_price", "cache_write_price", "cache_price")
            if model.get(key)
        }
        if model.get("post_multiplier_pricing") and source_group:
            source_multiplier = groups.get(source_group, 1.0)
            for price_key, price_value in list(base_prices.items()):
                base_prices[price_key] = revert_post_multiplier_price(price_value, source_multiplier)

        for group, multiplier in target_groups.items():
            pricing = {
                "model_name": model.get("model_name"),
                "group": group,
                "multiplier": multiplier,
                "group_note": group_notes.get(group) or model.get("group_note") or "",
            }
            pricing.update(base_prices)
            entries.append(
                {
                    "station": station,
                    "pricing": pricing,
                    "source": "batch-ingest",
                }
            )

    return {
        "station": station,
        "entries": entries,
    }


def ingest_batch(text: str) -> Dict[str, Any]:
    parsed = build_batch_payload(text)
    with registry_write_lock():
        registry = load_registry()
        results = []
        for entry in parsed["entries"]:
            results.append(upsert_record(registry, entry))
    summary = build_write_summary(results[-1]["station"]) if results else None
    return {
        "station": parsed["station"],
        "count": len(results),
        "summary": summary,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch ingest site price text.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--text", help="Raw batch text")
    source_group.add_argument("--text-file", help="Path to raw batch text file")
    args = parser.parse_args()

    text = args.text if args.text else read_text(args.text_file)
    print(json.dumps(ingest_batch(text), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
