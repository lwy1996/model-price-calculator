from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import mysql.connector
except ModuleNotFoundError:
    mysql = None


ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG_PATH = ROOT / "config" / "storage.local.json"
EXAMPLE_CONFIG_PATH = ROOT / "config" / "storage.example.json"


def load_storage_config() -> Dict[str, Any]:
    path = Path(os.environ.get("MPC_STORAGE_CONFIG", "")).expanduser() if os.environ.get("MPC_STORAGE_CONFIG") else None
    candidates = [item for item in [path, LOCAL_CONFIG_PATH, EXAMPLE_CONFIG_PATH] if item]
    for candidate in candidates:
        if candidate.exists():
            with open(candidate, "r", encoding="utf-8-sig") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                raise RuntimeError(f"存储配置文件顶层必须是对象：{candidate}")
            return data
    return {}


def env_or_config(env_key: str, config: Dict[str, Any], config_key: str, default: Any = "") -> Any:
    env_value = os.environ.get(env_key)
    if env_value not in (None, ""):
        return env_value
    return (config.get("mysql") or {}).get(config_key, default)


def mysql_enabled() -> bool:
    return True


def connect():
    if mysql is None:
        raise RuntimeError("当前环境未安装 mysql-connector-python，无法启用 MySQL 存储")
    config = load_storage_config()
    host = env_or_config("MPC_DB_HOST", config, "host")
    user = env_or_config("MPC_DB_USER", config, "user")
    database = env_or_config("MPC_DB_NAME", config, "database")
    port = env_or_config("MPC_DB_PORT", config, "port", 3306)
    password = env_or_config("MPC_DB_PASSWORD", config, "password", "")
    missing = []
    if not host:
        missing.append("MPC_DB_HOST / mysql.host")
    if not user:
        missing.append("MPC_DB_USER / mysql.user")
    if not database:
        missing.append("MPC_DB_NAME / mysql.database")
    if missing:
        raise RuntimeError("MySQL 存储已启用，但缺少配置：" + ", ".join(missing))
    return mysql.connector.connect(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        use_unicode=True,
    )


def query_all(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    db = connect()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        db.close()


def dt_to_iso(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def iso_to_mysql(value: Any, fallback: Optional[str] = None) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def now_mysql() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decimal_or_none(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def metric(record: Dict[str, Any], dimension: str) -> Optional[Decimal]:
    dimension_value = ((record.get("computed") or {}).get("dimensions") or {}).get(dimension)
    if not isinstance(dimension_value, dict):
        return None
    return decimal_or_none(dimension_value.get("rmb_per_m"))


def summary_metric(record: Dict[str, Any]) -> Optional[Decimal]:
    summary = (record.get("computed") or {}).get("summary") or {}
    return decimal_or_none(summary.get("rmb_per_m")) if isinstance(summary, dict) else None


def active_ids(items: Iterable[Dict[str, Any]], key: str) -> List[str]:
    return [str(item.get(key)) for item in items if item.get(key) not in (None, "")]


def sql_placeholders(values: Sequence[Any]) -> str:
    return ", ".join(["%s"] * len(values))


def soft_delete_missing(cursor, table: str, identity_column: str, keep_values: Sequence[str]) -> None:
    if keep_values:
        cursor.execute(
            f"UPDATE `{table}` SET deleted_at = NOW(), delete_token = CONCAT('deleted:', id) "
            f"WHERE deleted_at IS NULL AND `{identity_column}` NOT IN ({sql_placeholders(keep_values)})",
            tuple(keep_values),
        )
    else:
        cursor.execute(
            f"UPDATE `{table}` SET deleted_at = NOW(), delete_token = CONCAT('deleted:', id) "
            "WHERE deleted_at IS NULL"
        )


def load_registry() -> Dict[str, Any]:
    db = connect()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM mpc_stations WHERE deleted_at IS NULL ORDER BY id")
        stations = []
        for row in cursor.fetchall():
            station = {
                "station_id": row["station_id"],
                "name": row.get("name") or "",
                "aliases": [],
                "website": row.get("website") or "",
                "invite_url": row.get("invite_url") or "",
                "is_checked": bool(row.get("is_checked")),
                "checked_at": dt_to_iso(row.get("checked_at")),
                "recharge_ratio": row.get("recharge_ratio") or "1:1",
                "notes": row.get("notes") or "",
                "created_at": dt_to_iso(row.get("created_at")),
                "updated_at": dt_to_iso(row.get("updated_at")),
            }
            stations.append(station)

        stations_by_id = {station["station_id"]: station for station in stations}

        cursor.execute("SELECT station_id, alias FROM mpc_station_aliases WHERE deleted_at IS NULL ORDER BY id")
        for row in cursor.fetchall():
            station = stations_by_id.get(row["station_id"])
            if station is None:
                continue
            alias = row.get("alias") or ""
            if alias and alias not in station["aliases"]:
                station["aliases"].append(alias)

        cursor.execute(
            "SELECT station_id, group_name, multiplier FROM mpc_station_group_multipliers "
            "WHERE deleted_at IS NULL ORDER BY id"
        )
        for row in cursor.fetchall():
            station = stations_by_id.get(row["station_id"])
            if station is None:
                continue
            station.setdefault("group_multipliers", {})[row["group_name"]] = float(row["multiplier"])

        cursor.execute("SELECT * FROM mpc_price_records WHERE deleted_at IS NULL ORDER BY id")
        records = []
        for row in cursor.fetchall():
            record = {
                "record_id": row["record_id"],
                "station_id": row["station_id"],
                "model_name": row["model_name"],
                "group": row.get("group_name") or "default",
                "source": row.get("source") or "",
                "currency_hint": row.get("currency_hint") or "",
                "input_price": row.get("input_price") or "",
                "output_price": row.get("output_price") or "",
                "cache_price": row.get("cache_price"),
                "cache_read_price": row.get("cache_read_price"),
                "cache_write_price": row.get("cache_write_price"),
                "multiplier": float(row.get("multiplier") or 1),
                "recharge_ratio": row.get("recharge_ratio") or "1:1",
                "sale_price": row.get("sale_price"),
                "tags": json_loads(row.get("tags_json"), []),
                "computed": json_loads(row.get("computed_json"), {}),
                "updated_at": dt_to_iso(row.get("updated_at")),
                "created_at": dt_to_iso(row.get("created_at")),
            }
            if row.get("group_note") is not None:
                record["group_note"] = row.get("group_note")
            if row.get("last_verified_at") is not None:
                record["last_verified_at"] = dt_to_iso(row.get("last_verified_at"))
            if row.get("expires_at") is not None:
                record["expires_at"] = dt_to_iso(row.get("expires_at"))
            if row.get("stale_after_days") is not None:
                record["stale_after_days"] = int(row["stale_after_days"])
            records.append(record)

        return {"version": 1, "stations": stations, "price_records": records}
    finally:
        db.close()


def save_registry(registry: Dict[str, Any]) -> None:
    db = connect()
    try:
        cursor = db.cursor()
        stations = registry.get("stations") or []
        records = registry.get("price_records") or []

        soft_delete_missing(cursor, "mpc_stations", "station_id", active_ids(stations, "station_id"))
        soft_delete_missing(cursor, "mpc_price_records", "record_id", active_ids(records, "record_id"))

        for station in stations:
            station_id = station.get("station_id")
            if not station_id:
                continue
            created_at = iso_to_mysql(station.get("created_at"), now_mysql())
            updated_at = iso_to_mysql(station.get("updated_at"), created_at)
            cursor.execute(
                """
                INSERT INTO mpc_stations
                    (station_id, name, website, invite_url, is_checked, checked_at, recharge_ratio, notes, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    website = VALUES(website),
                    invite_url = VALUES(invite_url),
                    is_checked = VALUES(is_checked),
                    checked_at = VALUES(checked_at),
                    recharge_ratio = VALUES(recharge_ratio),
                    notes = VALUES(notes),
                    updated_at = VALUES(updated_at),
                    deleted_at = NULL,
                    delete_token = ''
                """,
                (
                    station_id,
                    station.get("name") or "",
                    station.get("website") or "",
                    station.get("invite_url") or "",
                    1 if station.get("is_checked") else 0,
                    iso_to_mysql(station.get("checked_at")),
                    station.get("recharge_ratio") or "1:1",
                    station.get("notes"),
                    created_at,
                    updated_at,
                ),
            )

            aliases = set(str(value).strip() for value in station.get("aliases") or [] if str(value).strip())
            for value in (station.get("name"), station.get("website")):
                if value:
                    aliases.add(str(value).strip())
            if aliases:
                cursor.execute(
                    f"UPDATE mpc_station_aliases SET deleted_at = NOW(), delete_token = CONCAT('deleted:', id) "
                    f"WHERE deleted_at IS NULL AND station_id = %s AND alias NOT IN ({sql_placeholders(list(aliases))})",
                    tuple([station_id] + list(aliases)),
                )
            else:
                cursor.execute(
                    "UPDATE mpc_station_aliases SET deleted_at = NOW(), delete_token = CONCAT('deleted:', id) "
                    "WHERE deleted_at IS NULL AND station_id = %s",
                    (station_id,),
                )
            for alias in aliases:
                cursor.execute(
                    """
                    INSERT INTO mpc_station_aliases (station_id, alias, created_at, updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE station_id = VALUES(station_id), updated_at = VALUES(updated_at),
                        deleted_at = NULL, delete_token = ''
                    """,
                    (station_id, alias, created_at, updated_at),
                )

            multipliers = station.get("group_multipliers") or {}
            groups = [str(key) for key in multipliers.keys()]
            if groups:
                cursor.execute(
                    f"UPDATE mpc_station_group_multipliers SET deleted_at = NOW(), delete_token = CONCAT('deleted:', id) "
                    f"WHERE deleted_at IS NULL AND station_id = %s AND group_name NOT IN ({sql_placeholders(groups)})",
                    tuple([station_id] + groups),
                )
            else:
                cursor.execute(
                    "UPDATE mpc_station_group_multipliers SET deleted_at = NOW(), delete_token = CONCAT('deleted:', id) "
                    "WHERE deleted_at IS NULL AND station_id = %s",
                    (station_id,),
                )
            for group_name, multiplier in multipliers.items():
                cursor.execute(
                    """
                    INSERT INTO mpc_station_group_multipliers
                        (station_id, group_name, multiplier, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE multiplier = VALUES(multiplier), updated_at = VALUES(updated_at),
                        deleted_at = NULL, delete_token = ''
                    """,
                    (station_id, group_name, decimal_or_none(multiplier), created_at, updated_at),
                )

        for record in records:
            record_id = record.get("record_id")
            if not record_id:
                continue
            created_at = iso_to_mysql(record.get("created_at"), now_mysql())
            updated_at = iso_to_mysql(record.get("updated_at"), created_at)
            cursor.execute(
                """
                INSERT INTO mpc_price_records
                    (record_id, station_id, model_name, group_name, group_note, source, currency_hint,
                     input_price, output_price, cache_price, cache_read_price, cache_write_price,
                     multiplier, recharge_ratio, sale_price, tags_json, computed_json,
                     input_rmb_per_m, output_rmb_per_m, cache_read_rmb_per_m, summary_rmb_per_m,
                     last_verified_at, expires_at, stale_after_days, created_at, updated_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    group_note = VALUES(group_note),
                    source = VALUES(source),
                    currency_hint = VALUES(currency_hint),
                    input_price = VALUES(input_price),
                    output_price = VALUES(output_price),
                    cache_price = VALUES(cache_price),
                    cache_read_price = VALUES(cache_read_price),
                    cache_write_price = VALUES(cache_write_price),
                    multiplier = VALUES(multiplier),
                    recharge_ratio = VALUES(recharge_ratio),
                    sale_price = VALUES(sale_price),
                    tags_json = VALUES(tags_json),
                    computed_json = VALUES(computed_json),
                    input_rmb_per_m = VALUES(input_rmb_per_m),
                    output_rmb_per_m = VALUES(output_rmb_per_m),
                    cache_read_rmb_per_m = VALUES(cache_read_rmb_per_m),
                    summary_rmb_per_m = VALUES(summary_rmb_per_m),
                    last_verified_at = VALUES(last_verified_at),
                    expires_at = VALUES(expires_at),
                    stale_after_days = VALUES(stale_after_days),
                    updated_at = VALUES(updated_at),
                    deleted_at = NULL,
                    delete_token = ''
                """,
                (
                    record_id,
                    record.get("station_id") or "",
                    record.get("model_name") or "",
                    record.get("group") or "default",
                    record.get("group_note"),
                    record.get("source") or "",
                    record.get("currency_hint") or "",
                    record.get("input_price") or "",
                    record.get("output_price") or "",
                    record.get("cache_price"),
                    record.get("cache_read_price"),
                    record.get("cache_write_price"),
                    decimal_or_none(record.get("multiplier")) or Decimal("1"),
                    record.get("recharge_ratio") or "1:1",
                    record.get("sale_price"),
                    json_dumps(record.get("tags") or []),
                    json_dumps(record.get("computed")),
                    metric(record, "input"),
                    metric(record, "output"),
                    metric(record, "cache_read"),
                    summary_metric(record),
                    iso_to_mysql(record.get("last_verified_at")),
                    iso_to_mysql(record.get("expires_at")),
                    int(record["stale_after_days"]) if record.get("stale_after_days") not in (None, "") else None,
                    created_at,
                    updated_at,
                ),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def load_history() -> Dict[str, Any]:
    rows = query_all("SELECT * FROM mpc_price_history WHERE deleted_at IS NULL ORDER BY id")
    changes = []
    for row in rows:
        changes.append(
            {
                "_history_id": row["id"],
                "changed_at": dt_to_iso(row.get("changed_at")),
                "source": row.get("source") or "",
                "record_id": row.get("record_id") or "",
                "station_id": row.get("station_id") or "",
                "model_name": row.get("model_name") or "",
                "group": row.get("group_name") or "default",
                "changed_fields": json_loads(row.get("changed_fields_json"), []),
                "old": json_loads(row.get("old_json"), {}),
                "new": json_loads(row.get("new_json"), {}),
                "summary_change_percent": None if row.get("summary_change_percent") is None else str(row.get("summary_change_percent")),
            }
        )
    return {"version": 1, "changes": changes}


def save_history(history: Dict[str, Any]) -> None:
    db = connect()
    try:
        cursor = db.cursor()
        for change in history.get("changes") or []:
            if change.get("_history_id"):
                continue
            changed_at = iso_to_mysql(change.get("changed_at"), now_mysql())
            cursor.execute(
                """
                INSERT INTO mpc_price_history
                    (changed_at, source, record_id, station_id, model_name, group_name,
                     changed_fields_json, old_json, new_json, summary_change_percent, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    changed_at,
                    change.get("source") or "",
                    change.get("record_id") or "",
                    change.get("station_id") or "",
                    change.get("model_name") or "",
                    change.get("group") or "default",
                    json_dumps(change.get("changed_fields") or []),
                    json_dumps(change.get("old") or {}),
                    json_dumps(change.get("new") or {}),
                    decimal_or_none(change.get("summary_change_percent")),
                    changed_at,
                ),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def load_drafts() -> Dict[str, Any]:
    rows = query_all("SELECT * FROM mpc_drafts WHERE deleted_at IS NULL ORDER BY id")
    drafts = []
    for row in rows:
        draft = {
            "draft_id": row.get("draft_id") or "",
            "station": json_loads(row.get("station_json"), {}),
            "pricing": json_loads(row.get("pricing_json"), {}),
            "notes": row.get("notes") or "",
            "created_at": dt_to_iso(row.get("created_at")),
            "updated_at": dt_to_iso(row.get("updated_at")),
        }
        if row.get("raw_text"):
            draft["raw_text"] = row.get("raw_text")
        drafts.append(draft)
    return {"version": 1, "drafts": drafts}


def save_drafts(data: Dict[str, Any]) -> None:
    db = connect()
    try:
        cursor = db.cursor()
        drafts = data.get("drafts") or []
        soft_delete_missing(cursor, "mpc_drafts", "draft_id", active_ids(drafts, "draft_id"))
        for draft in drafts:
            draft_id = draft.get("draft_id")
            if not draft_id:
                continue
            created_at = iso_to_mysql(draft.get("created_at"), now_mysql())
            updated_at = iso_to_mysql(draft.get("updated_at"), created_at)
            cursor.execute(
                """
                INSERT INTO mpc_drafts
                    (draft_id, station_json, pricing_json, raw_text, notes, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    station_json = VALUES(station_json),
                    pricing_json = VALUES(pricing_json),
                    raw_text = VALUES(raw_text),
                    notes = VALUES(notes),
                    updated_at = VALUES(updated_at),
                    deleted_at = NULL,
                    delete_token = ''
                """,
                (
                    draft_id,
                    json_dumps(draft.get("station") or {}),
                    json_dumps(draft.get("pricing") or {}),
                    draft.get("raw_text"),
                    draft.get("notes"),
                    created_at,
                    updated_at,
                ),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def load_model_catalog() -> Dict[str, Any]:
    db = connect()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM mpc_models WHERE deleted_at IS NULL ORDER BY id")
        models = []
        for row in cursor.fetchall():
            defaults = {}
            if row.get("default_input_price"):
                defaults["input_price"] = row.get("default_input_price")
            if row.get("default_cache_read_price"):
                defaults["cache_read_price"] = row.get("default_cache_read_price")
            if row.get("default_output_price"):
                defaults["output_price"] = row.get("default_output_price")
            if row.get("default_cache_write_price"):
                defaults["cache_write_price"] = row.get("default_cache_write_price")
            models.append({"canonical": row["canonical"], "aliases": [], "defaults": defaults})

        models_by_name = {model["canonical"]: model for model in models}
        cursor.execute("SELECT canonical, alias FROM mpc_model_aliases WHERE deleted_at IS NULL ORDER BY id")
        for row in cursor.fetchall():
            model = models_by_name.get(row["canonical"])
            if model is None:
                continue
            alias = row.get("alias") or ""
            if alias and alias not in model["aliases"]:
                model["aliases"].append(alias)
        return {"version": 1, "models": models}
    finally:
        db.close()
