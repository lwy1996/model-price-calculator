#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "assets" / "site-price-registry.json"
HISTORY_PATH = ROOT / "assets" / "site-price-history.json"
DRAFTS_PATH = ROOT / "assets" / "site-price-drafts.json"
MODEL_CATALOG_PATH = ROOT / "assets" / "model-catalog.json"
DEFAULT_OUTPUT_PATH = ROOT / "runtime" / "mysql-seed-current-data.sql"


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return data


def sql_string(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def sql_text(value: Any) -> str:
    if value in (None, ""):
        return "NULL"
    return sql_string(value)


def sql_json(value: Any) -> str:
    if value in (None, ""):
        return "NULL"
    return sql_string(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def sql_decimal(value: Any) -> str:
    if value in (None, ""):
        return "NULL"
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return "NULL"


def sql_int(value: Any) -> str:
    if value in (None, ""):
        return "NULL"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "NULL"


def mysql_datetime(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def sql_datetime(value: Any, fallback: Optional[str] = None) -> str:
    converted = mysql_datetime(value) or fallback
    return sql_string(converted) if converted else "NULL"


def compact_unique(values: Iterable[Any]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        text = "" if value is None else str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def emit_insert(table: str, columns: List[str], values: List[str], updates: List[str]) -> str:
    column_sql = ", ".join(f"`{column}`" for column in columns)
    value_sql = ", ".join(values)
    update_sql = ", ".join(f"`{column}` = VALUES(`{column}`)" for column in updates)
    return f"INSERT INTO `{table}` ({column_sql}) VALUES ({value_sql}) ON DUPLICATE KEY UPDATE {update_sql};"


def record_metric(record: Dict[str, Any], dimension: str, key: str = "rmb_per_m") -> str:
    dimension_value = ((record.get("computed") or {}).get("dimensions") or {}).get(dimension)
    if not isinstance(dimension_value, dict):
        return "NULL"
    return sql_decimal(dimension_value.get(key))


def record_summary(record: Dict[str, Any]) -> str:
    return sql_decimal(((record.get("computed") or {}).get("summary") or {}).get("rmb_per_m"))


def history_value(change: Dict[str, Any], key: str) -> str:
    value = change.get(key)
    return sql_json(value)


def build_station_sql(registry: Dict[str, Any]) -> List[str]:
    lines = []
    for station in registry.get("stations") or []:
        if not isinstance(station, dict):
            continue
        created_at = mysql_datetime(station.get("created_at")) or "1970-01-01 00:00:00"
        updated_at = mysql_datetime(station.get("updated_at")) or created_at
        columns = [
            "station_id",
            "name",
            "website",
            "invite_url",
            "recharge_ratio",
            "notes",
            "created_at",
            "updated_at",
        ]
        values = [
            sql_string(station.get("station_id") or ""),
            sql_string(station.get("name") or ""),
            sql_string(station.get("website") or ""),
            sql_string(station.get("invite_url") or ""),
            sql_string(station.get("recharge_ratio") or "1:1"),
            sql_text(station.get("notes")),
            sql_string(created_at),
            sql_string(updated_at),
        ]
        lines.append(emit_insert("mpc_stations", columns, values, ["name", "website", "invite_url", "recharge_ratio", "notes", "updated_at"]))

        aliases = compact_unique([station.get("name"), station.get("website")] + list(station.get("aliases") or []))
        for alias in aliases:
            alias_columns = ["station_id", "alias", "created_at", "updated_at"]
            alias_values = [sql_string(station.get("station_id") or ""), sql_string(alias), sql_string(created_at), sql_string(updated_at)]
            lines.append(emit_insert("mpc_station_aliases", alias_columns, alias_values, ["station_id", "updated_at"]))

        multipliers = station.get("group_multipliers") or {}
        if isinstance(multipliers, dict):
            for group_name, multiplier in multipliers.items():
                group_columns = ["station_id", "group_name", "multiplier", "created_at", "updated_at"]
                group_values = [
                    sql_string(station.get("station_id") or ""),
                    sql_string(group_name),
                    sql_decimal(multiplier),
                    sql_string(created_at),
                    sql_string(updated_at),
                ]
                lines.append(emit_insert("mpc_station_group_multipliers", group_columns, group_values, ["multiplier", "updated_at"]))
    return lines


def build_price_record_sql(registry: Dict[str, Any]) -> List[str]:
    lines = []
    for record in registry.get("price_records") or []:
        if not isinstance(record, dict):
            continue
        created_at = mysql_datetime(record.get("created_at")) or "1970-01-01 00:00:00"
        updated_at = mysql_datetime(record.get("updated_at")) or created_at
        columns = [
            "record_id",
            "station_id",
            "model_name",
            "group_name",
            "group_note",
            "source",
            "currency_hint",
            "input_price",
            "output_price",
            "cache_price",
            "cache_read_price",
            "cache_write_price",
            "multiplier",
            "recharge_ratio",
            "sale_price",
            "tags_json",
            "computed_json",
            "input_rmb_per_m",
            "output_rmb_per_m",
            "cache_read_rmb_per_m",
            "summary_rmb_per_m",
            "last_verified_at",
            "expires_at",
            "stale_after_days",
            "created_at",
            "updated_at",
        ]
        values = [
            sql_string(record.get("record_id") or ""),
            sql_string(record.get("station_id") or ""),
            sql_string(record.get("model_name") or ""),
            sql_string(record.get("group") or "default"),
            sql_text(record.get("group_note")),
            sql_string(record.get("source") or ""),
            sql_string(record.get("currency_hint") or ""),
            sql_string(record.get("input_price") or ""),
            sql_string(record.get("output_price") or ""),
            sql_text(record.get("cache_price")),
            sql_text(record.get("cache_read_price")),
            sql_text(record.get("cache_write_price")),
            sql_decimal(record.get("multiplier") if record.get("multiplier") not in (None, "") else "1"),
            sql_string(record.get("recharge_ratio") or "1:1"),
            sql_text(record.get("sale_price")),
            sql_json(record.get("tags") or []),
            sql_json(record.get("computed")),
            record_metric(record, "input"),
            record_metric(record, "output"),
            record_metric(record, "cache_read"),
            record_summary(record),
            sql_datetime(record.get("last_verified_at")),
            sql_datetime(record.get("expires_at")),
            sql_int(record.get("stale_after_days")),
            sql_string(created_at),
            sql_string(updated_at),
        ]
        updates = [column for column in columns if column not in {"record_id", "station_id", "model_name", "group_name", "created_at"}]
        lines.append(emit_insert("mpc_price_records", columns, values, updates))
    return lines


def build_history_sql(history: Dict[str, Any]) -> List[str]:
    lines = []
    for change in history.get("changes") or []:
        if not isinstance(change, dict):
            continue
        changed_at = mysql_datetime(change.get("changed_at")) or "1970-01-01 00:00:00"
        columns = [
            "changed_at",
            "source",
            "record_id",
            "station_id",
            "model_name",
            "group_name",
            "changed_fields_json",
            "old_json",
            "new_json",
            "summary_change_percent",
            "created_at",
        ]
        values = [
            sql_string(changed_at),
            sql_string(change.get("source") or ""),
            sql_string(change.get("record_id") or ""),
            sql_string(change.get("station_id") or ""),
            sql_string(change.get("model_name") or ""),
            sql_string(change.get("group") or "default"),
            sql_json(change.get("changed_fields") or []),
            history_value(change, "old"),
            history_value(change, "new"),
            sql_decimal(change.get("summary_change_percent")),
            sql_string(changed_at),
        ]
        column_sql = ", ".join(f"`{column}`" for column in columns)
        value_sql = ", ".join(values)
        lines.append(f"INSERT INTO `mpc_price_history` ({column_sql}) VALUES ({value_sql});")
    return lines


def build_draft_sql(drafts: Dict[str, Any]) -> List[str]:
    lines = []
    for draft in drafts.get("drafts") or []:
        if not isinstance(draft, dict):
            continue
        created_at = mysql_datetime(draft.get("created_at")) or "1970-01-01 00:00:00"
        updated_at = mysql_datetime(draft.get("updated_at")) or created_at
        columns = ["draft_id", "station_json", "pricing_json", "raw_text", "notes", "created_at", "updated_at"]
        values = [
            sql_string(draft.get("draft_id") or ""),
            sql_json(draft.get("station") or {}),
            sql_json(draft.get("pricing") or {}),
            sql_text(draft.get("raw_text")),
            sql_text(draft.get("notes")),
            sql_string(created_at),
            sql_string(updated_at),
        ]
        lines.append(emit_insert("mpc_drafts", columns, values, ["station_json", "pricing_json", "raw_text", "notes", "updated_at"]))
    return lines


def build_model_sql(catalog: Dict[str, Any]) -> List[str]:
    lines = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for model in catalog.get("models") or []:
        if not isinstance(model, dict):
            continue
        defaults = model.get("defaults") or {}
        canonical = model.get("canonical") or ""
        columns = [
            "canonical",
            "default_input_price",
            "default_output_price",
            "default_cache_read_price",
            "default_cache_write_price",
            "created_at",
            "updated_at",
        ]
        values = [
            sql_string(canonical),
            sql_text(defaults.get("input_price")),
            sql_text(defaults.get("output_price")),
            sql_text(defaults.get("cache_read_price")),
            sql_text(defaults.get("cache_write_price")),
            sql_string(now),
            sql_string(now),
        ]
        lines.append(emit_insert("mpc_models", columns, values, columns[1:]))

        aliases = compact_unique([canonical] + list(model.get("aliases") or []))
        for alias in aliases:
            alias_columns = ["canonical", "alias", "created_at", "updated_at"]
            alias_values = [sql_string(canonical), sql_string(alias), sql_string(now), sql_string(now)]
            lines.append(emit_insert("mpc_model_aliases", alias_columns, alias_values, ["canonical", "updated_at"]))
    return lines


def build_seed_sql() -> str:
    registry = load_json(REGISTRY_PATH)
    history = load_json(HISTORY_PATH)
    drafts = load_json(DRAFTS_PATH)
    catalog = load_json(MODEL_CATALOG_PATH)

    lines = [
        "-- Generated by scripts/export_mysql_seed.py",
        "-- Review before executing against MySQL.",
        "SET NAMES utf8mb4;",
        "START TRANSACTION;",
        "",
    ]
    lines.extend(build_station_sql(registry))
    lines.extend(build_price_record_sql(registry))
    lines.extend(build_history_sql(history))
    lines.extend(build_draft_sql(drafts))
    lines.extend(build_model_sql(catalog))
    lines.extend(
        [
            "INSERT INTO `mpc_schema_migrations` (`version`, `description`, `executed_at`) VALUES ('seed-current-json', 'Seed current JSON data into MySQL', NOW()) ON DUPLICATE KEY UPDATE `executed_at` = VALUES(`executed_at`);",
            "COMMIT;",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export current JSON data as a MySQL seed SQL file.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output SQL file path")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_seed_sql(), encoding="utf-8")
    print(json.dumps({"ok": True, "output_file": str(output_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
