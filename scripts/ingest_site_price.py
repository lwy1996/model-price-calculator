#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from extract_model_price import extract_payload
from site_price_registry import load_json_file, load_registry, upsert_record


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
            "invite_url": args.invite_url,
            "is_checked": args.is_checked,
            "checked_at": args.checked_at,
            "last_check_latency_seconds": args.last_check_latency_seconds,
            "admin_notes": args.admin_notes,
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
    if args.group_note:
        raw["group_note"] = args.group_note
    return raw


def build_upsert_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    station = raw.get("station") or {}
    if raw.get("recharge_ratio"):
        station["recharge_ratio"] = raw["recharge_ratio"]
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
    if raw.get("sale_price"):
        pricing["sale_price"] = raw["sale_price"]
    if raw.get("group_note"):
        pricing["group_note"] = raw["group_note"]

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
    parser.add_argument("--invite-url", help="Site invite URL")
    parser.add_argument("--is-checked", action="store_true", help="Mark station as checked")
    parser.add_argument("--checked-at", help="Station checked time")
    parser.add_argument("--last-check-latency-seconds", type=int, help="Last check latency in seconds")
    parser.add_argument("--group", help="Model group")
    parser.add_argument("--model-name", help="Model name override")
    parser.add_argument("--multiplier", type=float, help="Multiplier override")
    parser.add_argument("--recharge-ratio", help="Recharge ratio override")
    parser.add_argument("--sale-price", help="Sale price override")
    parser.add_argument("--group-note", help="Per-group note override")
    parser.add_argument("--admin-notes", help="Station admin notes")
    parser.add_argument("--notes", help="Station notes")
    args = parser.parse_args()

    raw = load_input(args)
    upsert_payload = build_upsert_payload(raw)
    registry = load_registry()
    upsert_result = upsert_record(registry, upsert_payload)

    print(
        json.dumps(
            upsert_result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
