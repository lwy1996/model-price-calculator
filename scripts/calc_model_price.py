#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional


FOUR_DP = Decimal("0.0001")
PRICE_FIELD_KEYS = {
    "input_price": ("input", "input_price", "输入价格"),
    "output_price": ("output", "output_price", "补全价格", "输出价格"),
    "cache_price": ("cache", "cache_price", "缓存价格"),
    "cache_read_price": ("cache_read", "cache_read_price", "缓存读取价格"),
    "cache_write_price": ("cache_write", "cache_write_price", "缓存创建价格"),
}
OFFICIAL_MODEL_DEFAULTS = {
    "gpt-5-4": {
        "model_name": "gpt-5.4",
        "input_price": "2.50 / 1M",
        "cache_read_price": "0.25 / 1M",
        "output_price": "15.00 / 1M",
    },
    "gpt-5-5": {
        "model_name": "gpt-5.5",
        "input_price": "5.00 / 1M",
        "cache_read_price": "0.50 / 1M",
        "output_price": "30.00 / 1M",
    },
    "gpt-5-4-mini": {
        "model_name": "gpt-5.4-mini",
        "input_price": "0.75 / 1M",
        "cache_read_price": "0.075 / 1M",
        "output_price": "4.50 / 1M",
    },
}


def quantize_4(value: Decimal) -> Decimal:
    return value.quantize(FOUR_DP, rounding=ROUND_HALF_UP)


def to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    return Decimal(match.group(0))


def normalize_model_key(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace(".", "-")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def parse_ratio(value: Any) -> Dict[str, Decimal]:
    if value is None:
        value = "1:1"

    text = str(value).strip().replace("人民币/额度", "").replace("=", "").replace(" ", "")
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"无法解析充值比: {value}")

    left = to_decimal(parts[0])
    right = to_decimal(parts[1])
    if left is None or right is None or right == 0:
        raise ValueError(f"无法解析充值比: {value}")

    return {
        "display": f"{left}:{right}",
        "rmb": left,
        "credit": right,
        "unit_rate": left / right,
    }


def format_decimal(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return f"{quantize_4(value):f}"


def build_copy_text(model_name: str, group: str, values: Dict[str, Optional[Decimal]]) -> str:
    parts = [model_name]
    if group:
        parts.append(group)

    if values.get("input") is not None:
        parts.append(f"输入${format_decimal(values['input'])}")
    if values.get("output") is not None:
        parts.append(f"输出${format_decimal(values['output'])}")
    if values.get("cache_read") is not None:
        parts.append(f"缓存读${format_decimal(values['cache_read'])}")
    elif values.get("cache") is not None:
        parts.append(f"缓存${format_decimal(values['cache'])}")
    if values.get("cache_write") is not None:
        parts.append(f"缓存写${format_decimal(values['cache_write'])}")

    return "-".join(parts)


def calc_dimension(price: Optional[Decimal], multiplier: Decimal, unit_rate: Decimal) -> Optional[Dict[str, str]]:
    if price is None:
        return None

    multiplied = price * multiplier
    rmb_per_m = multiplied * unit_rate
    return {
        "usd_per_m": format_decimal(price),
        "multiplied_usd_per_m": format_decimal(multiplied),
        "rmb_per_m": format_decimal(rmb_per_m),
    }


def must_get(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def apply_official_model_defaults(payload: Dict[str, Any]) -> Dict[str, Any]:
    resolved = dict(payload)
    model_name = str(must_get(resolved, "model_name", "模型名称") or "gpt5.4")
    official = OFFICIAL_MODEL_DEFAULTS.get(normalize_model_key(model_name))
    if not official:
        return resolved

    resolved["model_name"] = official["model_name"]
    for field_name, keys in PRICE_FIELD_KEYS.items():
        current = must_get(resolved, *keys)
        if current in (None, "") and field_name in official:
            primary_key = keys[0]
            resolved[primary_key] = official[field_name]
    return resolved


def load_payload(raw: str) -> Dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象")
    return payload


def load_payload_from_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as file:
        return load_payload(file.read())


def compute(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = apply_official_model_defaults(payload)
    model_name = str(must_get(payload, "model_name", "模型名称") or "gpt5.4")
    group = str(must_get(payload, "group", "分组") or "")

    input_price = to_decimal(must_get(payload, "input", "input_price", "输入价格"))
    output_price = to_decimal(must_get(payload, "output", "output_price", "补全价格", "输出价格"))
    cache_price = to_decimal(must_get(payload, "cache", "cache_price", "缓存价格"))
    cache_read_price = to_decimal(must_get(payload, "cache_read", "cache_read_price", "缓存读取价格"))
    cache_write_price = to_decimal(must_get(payload, "cache_write", "cache_write_price", "缓存创建价格"))
    multiplier = to_decimal(must_get(payload, "multiplier", "倍率") or 1)
    ratio = parse_ratio(must_get(payload, "recharge_ratio", "充值比") or "1:1")

    if input_price is None:
        raise ValueError("缺少输入价格")
    if output_price is None:
        raise ValueError("缺少输出价格")
    if multiplier is None:
        multiplier = Decimal("1")

    effective_cache = cache_read_price or cache_price

    usd_values = {
        "input": input_price,
        "output": output_price,
        "cache": cache_price,
        "cache_read": cache_read_price,
        "cache_write": cache_write_price,
    }

    dimensions = {
        "input": calc_dimension(input_price, multiplier, ratio["unit_rate"]),
        "output": calc_dimension(output_price, multiplier, ratio["unit_rate"]),
        "cache": calc_dimension(cache_price, multiplier, ratio["unit_rate"]),
        "cache_read": calc_dimension(cache_read_price, multiplier, ratio["unit_rate"]),
        "cache_write": calc_dimension(cache_write_price, multiplier, ratio["unit_rate"]),
    }

    summary_parts = [input_price, output_price]
    if cache_read_price is not None:
        summary_parts.append(cache_read_price)
    elif cache_price is not None:
        summary_parts.append(cache_price)
    if cache_write_price is not None:
        summary_parts.append(cache_write_price)

    total_base = sum(summary_parts, Decimal("0"))
    total_multiplied = total_base * multiplier
    total_rmb = total_multiplied * ratio["unit_rate"]

    comparisons: Dict[str, Optional[str]] = {
        "output_input_ratio": format_decimal(output_price / input_price if input_price else None),
        "cache_read_discount_vs_input": format_decimal(
            (effective_cache / input_price) if effective_cache is not None and input_price else None
        ),
        "cache_write_ratio_vs_input": format_decimal(
            (cache_write_price / input_price) if cache_write_price is not None and input_price else None
        ),
    }

    sale_price = to_decimal(must_get(payload, "sale_price", "售价", "站点售价"))
    if sale_price is not None:
        profit = sale_price - total_base
        comparisons.update(
            {
                "sale_price_usd_per_m": format_decimal(sale_price),
                "gross_profit_usd_per_m": format_decimal(profit),
                "gross_margin": format_decimal((profit / sale_price) if sale_price else None),
                "payback_multiplier": format_decimal((sale_price / total_base) if total_base else None),
            }
        )

    return {
        "model_name": model_name,
        "group": group,
        "multiplier": format_decimal(multiplier),
        "recharge_ratio": {
            "display": ratio["display"],
            "unit_rate": format_decimal(ratio["unit_rate"]),
        },
        "copy_text": build_copy_text(model_name, group, usd_values),
        "dimensions": dimensions,
        "summary": {
            "base_usd_per_m": format_decimal(total_base),
            "multiplied_usd_per_m": format_decimal(total_multiplied),
            "rmb_per_m": format_decimal(total_rmb),
        },
        "comparisons": comparisons,
    }


def numeric_field(result: Dict[str, Any], *path: str) -> Optional[Decimal]:
    current: Any = result
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return to_decimal(current)


def plan_label(result: Dict[str, Any]) -> str:
    group = result.get("group") or "未分组"
    model_name = result.get("model_name") or "unknown-model"
    return f"{model_name}-{group}"


def rank_results(results: List[Dict[str, Any]], value_path: List[str], ascending: bool = True) -> List[Dict[str, str]]:
    scored = []
    for result in results:
        value = numeric_field(result, *value_path)
        if value is None:
            continue
        scored.append(
            {
                "plan": plan_label(result),
                "value": format_decimal(value),
                "_sort": value,
            }
        )

    scored.sort(key=lambda item: item["_sort"], reverse=not ascending)
    for item in scored:
        item.pop("_sort", None)
    return scored


def compare_many(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    rankings = {
        "input_rmb_low_to_high": rank_results(results, ["dimensions", "input", "rmb_per_m"]),
        "output_rmb_low_to_high": rank_results(results, ["dimensions", "output", "rmb_per_m"]),
        "summary_rmb_low_to_high": rank_results(results, ["summary", "rmb_per_m"]),
        "output_input_ratio_low_to_high": rank_results(results, ["comparisons", "output_input_ratio"]),
    }

    cache_candidates = []
    for result in results:
        for key in ("cache_read", "cache", "cache_write"):
            value = numeric_field(result, "dimensions", key, "rmb_per_m")
            if value is None:
                continue
            cache_candidates.append(
                {
                    "plan": plan_label(result),
                    "dimension": key,
                    "value": format_decimal(value),
                    "_sort": value,
                }
            )
    cache_candidates.sort(key=lambda item: item["_sort"])
    for item in cache_candidates:
        item.pop("_sort", None)

    return {
        "rankings": rankings,
        "cheapest_cache_option": cache_candidates[0] if cache_candidates else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate model token pricing metrics.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--json", help="JSON payload for pricing calculation")
    source_group.add_argument("--json-file", help="Path to a JSON file for pricing calculation")
    args = parser.parse_args()
    raw_payload = load_payload(args.json) if args.json else load_payload_from_file(args.json_file)
    if "plans" in raw_payload:
        plans = raw_payload.get("plans")
        if not isinstance(plans, list) or not plans:
            raise ValueError("plans 必须是非空数组")
        computed = [compute(plan) for plan in plans]
        result = {
            "plans": computed,
            "comparison": compare_many(computed),
        }
    else:
        result = compute(raw_payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
