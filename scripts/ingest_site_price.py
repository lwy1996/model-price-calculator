#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from extract_model_price import extract_payload
from site_price_registry import load_json_file, load_registry, rank_records, upsert_record


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig") as file:
        return file.read()


def load_input(args: argparse.Namespace) -> Dict[str, Any]:
    if args.json_file:
        return load_json_file(args.json_file)

    raw: Dict[str, Any] = {
        "station": {
            "alias": args.alias,
            "name": args.station_name,
            "website": args.website,
            "api_base_url": args.api_base_url,
            "notes": args.notes,
        }
    }

    if args.text_file:
        raw["raw_text"] = read_text(args.text_file)
    elif args.text:
        raw["raw_text"] = args.text

    if args.group:
        raw["group"] = args.group
    if args.model_name:
        raw["model_name"] = args.model_name
    if args.multiplier is not None:
        raw["multiplier"] = args.multiplier
    if args.recharge_ratio:
        raw["recharge_ratio"] = args.recharge_ratio
    if args.sale_price:
        raw["sale_price"] = args.sale_price
    return raw


def build_upsert_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    station = raw.get("station") or {}
    pricing = raw.get("pricing")
    if pricing is None:
        extracted = extract_payload(raw.get("raw_text") or "")
        pricing = extracted

    if raw.get("model_name"):
        pricing["model_name"] = raw["model_name"]
    if raw.get("group"):
        pricing["group"] = raw["group"]
    if raw.get("multiplier") is not None:
        pricing["multiplier"] = raw["multiplier"]
    if raw.get("recharge_ratio"):
        pricing["recharge_ratio"] = raw["recharge_ratio"]
    if raw.get("sale_price"):
        pricing["sale_price"] = raw["sale_price"]

    return {
        "station": station,
        "pricing": pricing,
        "source": raw.get("source") or "ingest",
        "tags": raw.get("tags") or [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest site price text into the registry.")
    parser.add_argument("--json-file", help="Path to a full JSON payload")
    parser.add_argument("--text", help="Raw pricing text")
    parser.add_argument("--text-file", help="Path to a raw pricing text file")
    parser.add_argument("--alias", help="Site alias")
    parser.add_argument("--station-name", help="Site display name")
    parser.add_argument("--website", help="Site website")
    parser.add_argument("--api-base-url", help="Site API base URL")
    parser.add_argument("--group", help="Model group")
    parser.add_argument("--model-name", help="Model name override")
    parser.add_argument("--multiplier", type=float, help="Multiplier override")
    parser.add_argument("--recharge-ratio", help="Recharge ratio override")
    parser.add_argument("--sale-price", help="Sale price override")
    parser.add_argument("--notes", help="Station notes")
    args = parser.parse_args()

    raw = load_input(args)
    upsert_payload = build_upsert_payload(raw)
    registry = load_registry()
    upsert_result = upsert_record(registry, upsert_payload)

    rank_payload = {
        "model_name": upsert_result["record"]["model_name"],
        "group": upsert_result["record"]["group"],
        "sort_by": "summary_rmb_per_m",
        "direction": "asc",
    }
    rank_result = rank_records(load_registry(), rank_payload)

    print(
        json.dumps(
            {
                "upsert": upsert_result,
                "rank": rank_result,
                "station_snapshot": upsert_result.get("station_snapshot"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
