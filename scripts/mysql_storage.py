from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

try:
    import mysql.connector
except ModuleNotFoundError:
    mysql = None


ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG_PATH = ROOT / "config" / "storage.local.json"
EXAMPLE_CONFIG_PATH = ROOT / "config" / "storage.example.json"
BEIJING_TZ = timezone(timedelta(hours=8))


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
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ).replace(microsecond=0).isoformat()


def iso_to_mysql(value: Any, fallback: Optional[str] = None) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    parsed = parsed.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def now_mysql() -> str:
    return datetime.now(BEIJING_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def normalize_mysql_text(value: Any) -> str:
    return str(value or "").strip()


def first_present(payload: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload.get(key)
    return None


def parse_optional_bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = normalize_mysql_text(value).lower()
    if text in {"1", "true", "yes", "y", "on", "enable", "enabled", "是", "启用", "置顶"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disable", "disabled", "否", "禁用", "不置顶"}:
        return False
    return default


def parse_optional_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"无法解析整数: {value}")


def validate_http_url(value: Any, field_name: str) -> Optional[str]:
    text = normalize_mysql_text(value)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} 必须是合法 http/https URL")
    return text


def parse_json_array(value: Any, field_name: str) -> List[Dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} 必须是 JSON 数组") from exc
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是数组")
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{field_name} 第 {index} 项必须是对象")
        normalized.append(item)
    return normalized


def normalize_daily_news_links(value: Any, field_name: str) -> Tuple[Optional[str], int]:
    items = parse_json_array(value, field_name)
    normalized = []
    for index, item in enumerate(items, start=1):
        url = validate_http_url(item.get("url") or item.get("URL") or item.get("链接"), f"{field_name}[{index}].url")
        if not url:
            raise ValueError(f"{field_name} 第 {index} 项缺少 url")
        title = normalize_mysql_text(item.get("title") or item.get("标题")) or url
        normalized.append({"title": title, "url": url})
    return (json_dumps(normalized) if normalized else None, len(normalized))


def normalize_daily_news_images(value: Any, field_name: str) -> Tuple[Optional[str], int]:
    items = parse_json_array(value, field_name)
    normalized = []
    for index, item in enumerate(items, start=1):
        url = validate_http_url(item.get("url") or item.get("URL") or item.get("图片"), f"{field_name}[{index}].url")
        if not url:
            raise ValueError(f"{field_name} 第 {index} 项缺少 url")
        alt = normalize_mysql_text(item.get("alt") or item.get("image_alt") or item.get("图片说明") or item.get("说明"))
        normalized.append({"url": url, "alt": alt})
    return (json_dumps(normalized) if normalized else None, len(normalized))


def normalize_daily_news_payload(payload: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, int]]:
    is_update = existing is not None
    defaults = {
        "published_at": now_mysql(),
        "status": "published",
        "is_pinned": 0,
        "sort_order": 0,
    }
    data: Dict[str, Any] = {}
    counts = {"extra_links": 0, "images": 0}

    field_aliases = {
        "title": ("title", "标题"),
        "content": ("content", "正文", "新闻正文"),
        "link_title": ("link_title", "链接标题", "主链接标题"),
        "source_name": ("source_name", "来源", "来源名称"),
        "image_alt": ("image_alt", "图片说明", "主图说明"),
    }
    for field, aliases in field_aliases.items():
        value = first_present(payload, aliases)
        if is_update and any(key in payload for key in aliases):
            if field == "title" and value in (None, ""):
                raise ValueError("新闻标题不能为空")
            data[field] = normalize_mysql_text(value) if value not in (None, "") else None
        elif value not in (None, ""):
            data[field] = normalize_mysql_text(value)
        elif not is_update and field == "title":
            raise ValueError("新闻录入缺少 title/标题")

    if not is_update and "title" not in data:
        raise ValueError("新闻录入缺少 title/标题")

    link_url = first_present(payload, ("link_url", "主链接", "链接地址"))
    if link_url not in (None, ""):
        data["link_url"] = validate_http_url(link_url, "link_url")
    elif not is_update:
        data["link_url"] = None

    image_url = first_present(payload, ("image_url", "主图", "主图地址"))
    if image_url not in (None, ""):
        data["image_url"] = validate_http_url(image_url, "image_url")
    elif not is_update:
        data["image_url"] = None

    published_at = first_present(payload, ("published_at", "发布时间"))
    if published_at not in (None, ""):
        data["published_at"] = iso_to_mysql(published_at)
        if not data["published_at"]:
            raise ValueError(f"无法解析发布时间: {published_at}")
    elif not is_update:
        data["published_at"] = defaults["published_at"]

    status = first_present(payload, ("status", "状态"))
    if status not in (None, ""):
        status_text = normalize_mysql_text(status)
        if status_text not in {"draft", "published", "hidden"}:
            raise ValueError("status 只能是 draft / published / hidden")
        data["status"] = status_text
    elif not is_update:
        data["status"] = defaults["status"]

    if any(key in payload for key in ("is_pinned", "是否置顶", "置顶")):
        data["is_pinned"] = 1 if parse_optional_bool(first_present(payload, ("is_pinned", "是否置顶", "置顶"))) else 0
    elif not is_update:
        data["is_pinned"] = defaults["is_pinned"]

    sort_order = first_present(payload, ("sort_order", "排序", "排序值"))
    if sort_order not in (None, ""):
        data["sort_order"] = parse_optional_int(sort_order)
    elif not is_update:
        data["sort_order"] = defaults["sort_order"]

    links_value = first_present(payload, ("extra_links", "扩展链接"))
    if links_value not in (None, ""):
        data["extra_links_json"], counts["extra_links"] = normalize_daily_news_links(links_value, "extra_links")
    elif "extra_links_json" in payload:
        data["extra_links_json"], counts["extra_links"] = normalize_daily_news_links(payload.get("extra_links_json"), "extra_links_json")
    elif not is_update:
        data["extra_links_json"] = None

    images_value = first_present(payload, ("images", "扩展图片"))
    if images_value not in (None, ""):
        data["images_json"], counts["images"] = normalize_daily_news_images(images_value, "images")
    elif "images_json" in payload:
        data["images_json"], counts["images"] = normalize_daily_news_images(payload.get("images_json"), "images_json")
    elif not is_update:
        data["images_json"] = None

    if not is_update:
        for field in ("content", "link_title", "source_name", "image_alt"):
            data.setdefault(field, None)

    return data, counts


def upsert_daily_news(payload: Dict[str, Any]) -> Dict[str, Any]:
    news_id = first_present(payload, ("news_id", "id", "新闻ID"))
    db = connect()
    try:
        cursor = db.cursor(dictionary=True)
        existing = None
        if news_id not in (None, ""):
            cursor.execute(
                """
                SELECT *
                FROM site_price_daily_news
                WHERE id = %s
                  AND deleted_at IS NULL
                LIMIT 1
                """,
                (int(news_id),),
            )
            existing = cursor.fetchone()
            if not existing:
                raise ValueError(f"未找到可更新的每日新闻: {news_id}")

        data, counts = normalize_daily_news_payload(payload, existing)
        updated_at = now_mysql()
        if existing:
            if not data:
                raise ValueError("更新每日新闻时没有提供可修改字段")
            assignments = [f"`{field}` = %s" for field in data.keys()]
            values = list(data.values())
            assignments.append("`updated_at` = %s")
            values.append(updated_at)
            values.append(existing["id"])
            cursor.execute(
                f"UPDATE site_price_daily_news SET {', '.join(assignments)} WHERE id = %s AND deleted_at IS NULL",
                tuple(values),
            )
            saved_id = int(existing["id"])
            action = "updated"
        else:
            created_at = updated_at
            data["created_at"] = created_at
            data["updated_at"] = updated_at
            columns = list(data.keys())
            cursor.execute(
                f"INSERT INTO site_price_daily_news ({', '.join('`' + column + '`' for column in columns)}) "
                f"VALUES ({', '.join(['%s'] * len(columns))})",
                tuple(data[column] for column in columns),
            )
            saved_id = int(cursor.lastrowid)
            action = "created"

        cursor.execute("SELECT * FROM site_price_daily_news WHERE id = %s LIMIT 1", (saved_id,))
        saved = cursor.fetchone() or {}
        db.commit()
        counts["extra_links"] = len(json_loads(saved.get("extra_links_json"), []))
        counts["images"] = len(json_loads(saved.get("images_json"), []))
        link_count = counts["extra_links"] + (1 if saved.get("link_url") else 0)
        image_count = counts["images"] + (1 if saved.get("image_url") else 0)
        return {
            "action": action,
            "news_id": saved_id,
            "title": saved.get("title") or "",
            "status": saved.get("status") or "",
            "published_at": dt_to_iso(saved.get("published_at")),
            "is_pinned": bool(saved.get("is_pinned")),
            "sort_order": int(saved.get("sort_order") or 0),
            "link_url": saved.get("link_url") or "",
            "image_url": saved.get("image_url") or "",
            "link_count": link_count,
            "image_count": image_count,
            "extra_link_count": counts["extra_links"],
            "extra_image_count": counts["images"],
            "updated_at": dt_to_iso(saved.get("updated_at")),
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def upsert_probe_api_configs(configs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not configs:
        return []

    db = connect()
    try:
        cursor = db.cursor(dictionary=True)
        results: List[Dict[str, Any]] = []

        for config in configs:
            station_id = str(config.get("station_id") or "").strip()
            if not station_id:
                raise ValueError("probe 配置缺少 station_id")

            name = str(config.get("name") or "").strip()
            api_base_url = str(config.get("api_base_url") or "").strip()
            model = str(config.get("model") or "").strip()
            canonical_model_name = str(config.get("canonical_model_name") or model).strip()
            request_model_name = str(config.get("request_model_name") or model).strip()
            if not model:
                raise ValueError("probe 配置缺少 model")

            existing = None
            if api_base_url:
                cursor.execute(
                    """
                    SELECT id, config_id, created_at
                    FROM mpc_station_probe_api_configs
                    WHERE station_id = %s
                      AND api_base_url = %s
                      AND canonical_model_name = %s
                      AND deleted_at IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (station_id, api_base_url, canonical_model_name),
                )
                existing = cursor.fetchone()
                if not existing:
                    cursor.execute(
                        """
                        SELECT id, config_id, created_at
                        FROM mpc_station_probe_api_configs
                        WHERE station_id = %s
                          AND name = %s
                          AND canonical_model_name = %s
                          AND deleted_at IS NULL
                          AND (api_base_url = '' OR api_base_url IS NULL)
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (station_id, name, canonical_model_name),
                    )
                    existing = cursor.fetchone()
            else:
                cursor.execute(
                    """
                    SELECT id, config_id, created_at
                    FROM mpc_station_probe_api_configs
                    WHERE station_id = %s
                      AND name = %s
                      AND canonical_model_name = %s
                      AND deleted_at IS NULL
                      AND (api_base_url = '' OR api_base_url IS NULL)
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (station_id, name, canonical_model_name),
                )
                existing = cursor.fetchone()

            created_at = now_mysql()
            updated_at = now_mysql()

            if existing:
                config_id = existing.get("config_id") or config.get("config_id") or ""
                created_at = dt_to_iso(existing.get("created_at")) or created_at
                cursor.execute(
                    """
                    UPDATE mpc_station_probe_api_configs
                    SET
                        station_id = %s,
                        name = %s,
                        api_base_url = %s,
                        chat_completions_path = %s,
                        responses_path = %s,
                        responses_compact_path = %s,
                        api_key = %s,
                        model = %s,
                        canonical_model_name = %s,
                        request_model_name = %s,
                        is_enabled = %s,
                        last_success_endpoint_type = %s,
                        notes = %s,
                        updated_at = %s,
                        deleted_at = NULL
                    WHERE id = %s
                    """,
                    (
                        station_id,
                        name,
                        api_base_url,
                        config.get("chat_completions_path") or "/v1/chat/completions",
                        config.get("responses_path") or "/v1/responses",
                        config.get("responses_compact_path") or "/v1/responses/compact",
                        config.get("api_key"),
                        model,
                        canonical_model_name,
                        request_model_name,
                        1 if config.get("is_enabled") else 0,
                        config.get("last_success_endpoint_type") or "",
                        config.get("notes"),
                        updated_at,
                        existing["id"],
                    ),
                )
                action = "updated"
            else:
                config_id = str(config.get("config_id") or "").strip()
                cursor.execute(
                    """
                    INSERT INTO mpc_station_probe_api_configs
                        (config_id, station_id, name, api_base_url, chat_completions_path,
                         responses_path, responses_compact_path, api_key, model,
                         canonical_model_name, request_model_name, is_enabled,
                         last_success_endpoint_type, notes, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        config_id,
                        station_id,
                        name,
                        api_base_url,
                        config.get("chat_completions_path") or "/v1/chat/completions",
                        config.get("responses_path") or "/v1/responses",
                        config.get("responses_compact_path") or "/v1/responses/compact",
                        config.get("api_key"),
                        model,
                        canonical_model_name,
                        request_model_name,
                        1 if config.get("is_enabled") else 0,
                        config.get("last_success_endpoint_type") or "",
                        config.get("notes"),
                        created_at,
                        updated_at,
                    ),
                )
                action = "created"

            results.append(
                {
                    "action": action,
                    "config_id": config_id,
                    "station_id": station_id,
                    "name": name,
                    "api_base_url": api_base_url,
                    "model": model,
                    "canonical_model_name": canonical_model_name,
                    "request_model_name": request_model_name,
                    "is_enabled": bool(config.get("is_enabled")),
                    "notes": config.get("notes") or "",
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )

        db.commit()
        return results
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def soft_delete_probe_api_configs(filters: Dict[str, Any]) -> Dict[str, Any]:
    station_id = str(filters.get("station_id") or "").strip()
    if not station_id:
        raise ValueError("删除 probe 配置缺少 station_id")

    conditions = ["station_id = %s", "deleted_at IS NULL"]
    params: List[Any] = [station_id]
    for column, key in (
        ("config_id", "config_id"),
        ("name", "name"),
        ("api_base_url", "api_base_url"),
        ("canonical_model_name", "canonical_model_name"),
        ("request_model_name", "request_model_name"),
    ):
        if key in filters:
            conditions.append(f"{column} = %s")
            params.append(str(filters.get(key) or "").strip())

    db = connect()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            f"""
            SELECT config_id, station_id, name, api_base_url, canonical_model_name, request_model_name
            FROM mpc_station_probe_api_configs
            WHERE {" AND ".join(conditions)}
            ORDER BY id
            """,
            tuple(params),
        )
        removed = list(cursor.fetchall())
        if removed:
            cursor.execute("SHOW COLUMNS FROM `mpc_station_probe_api_configs` LIKE %s", ("delete_token",))
            delete_token_assignment = (
                ", delete_token = CONCAT('deleted:', id)"
                if cursor.fetchone() is not None
                else ""
            )
            cursor.execute(
                f"""
                UPDATE mpc_station_probe_api_configs
                SET deleted_at = %s{delete_token_assignment}
                WHERE {" AND ".join(conditions)}
                """,
                tuple([now_mysql()] + params),
            )
        db.commit()
        return {
            "action": "delete-probe-api-configs",
            "removed_count": len(removed),
            "removed_configs": removed,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def station_has_probe_api_config(station_id: str) -> bool:
    station_id = str(station_id or "").strip()
    if not station_id:
        return False

    rows = query_all(
        """
        SELECT id
        FROM mpc_station_probe_api_configs
        WHERE station_id = %s
          AND deleted_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (station_id,),
    )
    return bool(rows)


def station_has_balance_config(station_id: str) -> bool:
    station_id = str(station_id or "").strip()
    if not station_id:
        return False

    rows = query_all(
        """
        SELECT id
        FROM mpc_station_balance_configs
        WHERE station_id = %s
          AND deleted_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (station_id,),
    )
    return bool(rows)


def insert_balance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    station_id = str(config.get("station_id") or "").strip()
    if not station_id:
        raise ValueError("balance 配置缺少 station_id")

    db = connect()
    try:
        cursor = db.cursor()
        created_at = now_mysql()
        updated_at = now_mysql()
        cursor.execute(
            """
            INSERT INTO mpc_station_balance_configs
                (config_id, station_id, provider_type, base_url, access_token, user_id,
                 method, path, headers_json, remaining_path, used_path, total_path,
                 unit_path, plan_name_path, is_enabled, notes, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                config.get("config_id") or "",
                station_id,
                config.get("provider_type") or "",
                config.get("base_url") or "",
                config.get("access_token") or None,
                config.get("user_id") or "",
                config.get("method") or "GET",
                config.get("path") or "",
                config.get("headers_json"),
                config.get("remaining_path") or "",
                config.get("used_path") or "",
                config.get("total_path") or "",
                config.get("unit_path") or "",
                config.get("plan_name_path") or "",
                1 if config.get("is_enabled") else 0,
                config.get("notes"),
                created_at,
                updated_at,
            ),
        )
        db.commit()
        return {
            "action": "created",
            "config_id": config.get("config_id") or "",
            "station_id": station_id,
            "provider_type": config.get("provider_type") or "",
            "base_url": config.get("base_url") or "",
            "is_enabled": bool(config.get("is_enabled")),
            "user_id": config.get("user_id") or "",
            "notes": config.get("notes") or "",
            "created_at": created_at,
            "updated_at": updated_at,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def upsert_balance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    station_id = str(config.get("station_id") or "").strip()
    if not station_id:
        raise ValueError("balance 配置缺少 station_id")

    db = connect()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT id, config_id, created_at
            FROM mpc_station_balance_configs
            WHERE station_id = %s
              AND deleted_at IS NULL
            ORDER BY id ASC
            LIMIT 1
            """,
            (station_id,),
        )
        existing = cursor.fetchone()
        updated_at = now_mysql()
        if existing:
            config_id = existing.get("config_id") or config.get("config_id") or ""
            cursor.execute(
                """
                UPDATE mpc_station_balance_configs
                SET provider_type = %s,
                    base_url = %s,
                    access_token = %s,
                    user_id = %s,
                    method = %s,
                    path = %s,
                    headers_json = %s,
                    remaining_path = %s,
                    used_path = %s,
                    total_path = %s,
                    unit_path = %s,
                    plan_name_path = %s,
                    is_enabled = %s,
                    notes = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    config.get("provider_type") or "",
                    config.get("base_url") or "",
                    config.get("access_token") or None,
                    config.get("user_id") or "",
                    config.get("method") or "GET",
                    config.get("path") or "",
                    config.get("headers_json"),
                    config.get("remaining_path") or "",
                    config.get("used_path") or "",
                    config.get("total_path") or "",
                    config.get("unit_path") or "",
                    config.get("plan_name_path") or "",
                    1 if config.get("is_enabled") else 0,
                    config.get("notes"),
                    updated_at,
                    existing["id"],
                ),
            )
            action = "updated"
            created_at = dt_to_iso(existing.get("created_at"))
        else:
            saved = insert_balance_config(config)
            saved["action"] = "created"
            return saved

        db.commit()
        return {
            "action": action,
            "config_id": config_id,
            "station_id": station_id,
            "provider_type": config.get("provider_type") or "",
            "base_url": config.get("base_url") or "",
            "is_enabled": bool(config.get("is_enabled")),
            "user_id": config.get("user_id") or "",
            "notes": config.get("notes") or "",
            "created_at": created_at,
            "updated_at": updated_at,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sync_balance_base_url_by_website(station_id: str, old_website: str, new_website: str) -> Dict[str, Any]:
    station_id = str(station_id or "").strip()
    old_website = str(old_website or "").strip()
    new_website = str(new_website or "").strip()
    if not station_id or not old_website or not new_website or old_website == new_website:
        return {"matched": 0, "updated": 0, "station_id": station_id}

    db = connect()
    try:
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS matched_count
            FROM mpc_station_balance_configs
            WHERE station_id = %s
              AND deleted_at IS NULL
              AND base_url = %s
            """,
            (station_id, old_website),
        )
        row = cursor.fetchone()
        matched = int(row[0] if row else 0)

        cursor.execute(
            """
            UPDATE mpc_station_balance_configs
            SET base_url = %s,
                updated_at = %s
            WHERE station_id = %s
              AND deleted_at IS NULL
              AND base_url = %s
            """,
            (new_website, now_mysql(), station_id, old_website),
        )
        updated = int(cursor.rowcount or 0)
        db.commit()
        return {
            "matched": matched,
            "updated": updated,
            "station_id": station_id,
            "old_website": old_website,
            "new_website": new_website,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def bulk_guess_balance_provider_types(
    *,
    station_id: str = "",
    station_name: str = "",
    limit: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    station_id = str(station_id or "").strip()
    station_name = str(station_name or "").strip()
    limit_value = int(limit or 0)

    filters = [
        "bc.deleted_at IS NULL",
        "COALESCE(bc.provider_type, '') = ''",
    ]
    params: List[Any] = []
    if station_id:
        filters.append("bc.station_id = %s")
        params.append(station_id)
    if station_name:
        filters.append("s.name LIKE %s")
        params.append(f"%{station_name}%")

    candidates = query_all(
        f"""
        SELECT
            bc.id,
            bc.config_id,
            bc.station_id,
            bc.base_url,
            bc.notes,
            s.name AS station_name,
            s.website
        FROM mpc_station_balance_configs bc
        LEFT JOIN mpc_stations s
          ON s.station_id COLLATE utf8mb4_unicode_ci = bc.station_id COLLATE utf8mb4_unicode_ci
        WHERE {" AND ".join(filters)}
        ORDER BY bc.id ASC
        """,
        tuple(params),
    )
    if limit_value > 0:
        candidates = candidates[:limit_value]

    if not candidates:
        return {
            "action": "guess-balance-provider-types",
            "dry_run": dry_run,
            "scanned": 0,
            "matched": 0,
            "updated": 0,
            "items": [],
        }

    station_ids = [str(item.get("station_id") or "").strip() for item in candidates if str(item.get("station_id") or "").strip()]
    placeholders = ", ".join(["%s"] * len(station_ids))

    snapshots = query_all(
        f"""
        SELECT station_id, request_url, response_summary, last_probed_at
        FROM mpc_station_probe_snapshots
        WHERE station_id IN ({placeholders})
        ORDER BY last_probed_at DESC, id DESC
        """,
        tuple(station_ids),
    )
    logs = query_all(
        f"""
        SELECT station_id, request_url, response_summary, probed_at
        FROM mpc_station_probe_logs
        WHERE station_id IN ({placeholders})
          AND (
            INSTR(response_summary, 'New API') > 0
            OR INSTR(response_summary, 'Sub2API') > 0
            OR INSTR(LOWER(request_url), 'newapi') > 0
            OR INSTR(LOWER(request_url), 'newcli') > 0
            OR INSTR(LOWER(request_url), 'sub2api') > 0
          )
        ORDER BY probed_at DESC, id DESC
        """,
        tuple(station_ids),
    )
    probe_configs = query_all(
        f"""
        SELECT station_id, name, api_base_url, model, updated_at
        FROM mpc_station_probe_api_configs
        WHERE deleted_at IS NULL
          AND station_id IN ({placeholders})
        ORDER BY updated_at DESC, id DESC
        """,
        tuple(station_ids),
    )

    snapshot_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in snapshots:
        snapshot_map.setdefault(str(row.get("station_id") or "").strip(), []).append(row)
    log_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in logs:
        log_map.setdefault(str(row.get("station_id") or "").strip(), []).append(row)
    probe_config_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in probe_configs:
        probe_config_map.setdefault(str(row.get("station_id") or "").strip(), []).append(row)

    def normalize_text(value: Any) -> str:
        return str(value or "").strip().lower()

    def build_clues(row: Dict[str, Any]) -> List[Tuple[str, str, str]]:
        clues: List[Tuple[str, str, str]] = []
        station_key = str(row.get("station_id") or "").strip()
        for snapshot in snapshot_map.get(station_key, []):
            clues.append(
                (
                    "snapshot.response_summary",
                    str(snapshot.get("response_summary") or ""),
                    str(snapshot.get("last_probed_at") or ""),
                )
            )
            clues.append(
                (
                    "snapshot.request_url",
                    str(snapshot.get("request_url") or ""),
                    str(snapshot.get("last_probed_at") or ""),
                )
            )
        for log in log_map.get(station_key, [])[:10]:
            clues.append(
                (
                    "log.response_summary",
                    str(log.get("response_summary") or ""),
                    str(log.get("probed_at") or ""),
                )
            )
            clues.append(
                (
                    "log.request_url",
                    str(log.get("request_url") or ""),
                    str(log.get("probed_at") or ""),
                )
            )
        for probe_config in probe_config_map.get(station_key, []):
            clues.append(
                (
                    "probe_api.api_base_url",
                    str(probe_config.get("api_base_url") or ""),
                    str(probe_config.get("updated_at") or ""),
                )
            )
        clues.append(("station.website", str(row.get("website") or ""), ""))
        clues.append(("balance.base_url", str(row.get("base_url") or ""), ""))
        return clues

    def detect_provider_type(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        clues = build_clues(row)
        best: Optional[Dict[str, Any]] = None
        for source, raw_text, clue_time in clues:
            text = normalize_text(raw_text)
            if not text:
                continue
            current: Optional[Dict[str, Any]] = None
            if "sub2api" in text:
                current = {
                    "provider_type": "sub2api",
                    "confidence": "high",
                    "reason": f"{source} 命中 Sub2API",
                    "evidence": raw_text[:180],
                    "clue_time": clue_time,
                    "priority": 300,
                }
            elif "new api" in text:
                current = {
                    "provider_type": "newapi",
                    "confidence": "high",
                    "reason": f"{source} 命中 New API",
                    "evidence": raw_text[:180],
                    "clue_time": clue_time,
                    "priority": 260,
                }
            elif "newcli" in text:
                current = {
                    "provider_type": "newapi",
                    "confidence": "medium",
                    "reason": f"{source} 命中 newcli",
                    "evidence": raw_text[:180],
                    "clue_time": clue_time,
                    "priority": 230,
                }
            elif "newapi" in text:
                current = {
                    "provider_type": "newapi",
                    "confidence": "medium",
                    "reason": f"{source} 命中 newapi",
                    "evidence": raw_text[:180],
                    "clue_time": clue_time,
                    "priority": 220,
                }

            if current and (best is None or int(current["priority"]) > int(best["priority"])):
                best = current
        return best

    matched_items: List[Dict[str, Any]] = []
    for row in candidates:
        detected = detect_provider_type(row)
        if not detected:
            continue
        matched_items.append(
            {
                "id": row.get("id"),
                "config_id": row.get("config_id"),
                "station_id": row.get("station_id"),
                "station_name": row.get("station_name") or row.get("station_id"),
                "provider_type": detected["provider_type"],
                "confidence": detected["confidence"],
                "reason": detected["reason"],
                "evidence": detected["evidence"],
                "clue_time": detected["clue_time"],
                "base_url": row.get("base_url") or "",
                "old_notes": row.get("notes") or "",
            }
        )

    updated = 0
    if matched_items and not dry_run:
        db = connect()
        try:
            cursor = db.cursor()
            updated_at = now_mysql()
            for item in matched_items:
                auto_note = (
                    f"自动猜测 provider_type={item['provider_type']}"
                    f"（confidence={item['confidence']}; reason={item['reason']}）"
                )
                old_notes = str(item.get("old_notes") or "").strip()
                new_notes = auto_note if not old_notes else old_notes + "\n" + auto_note
                cursor.execute(
                    """
                    UPDATE mpc_station_balance_configs
                    SET provider_type = %s,
                        notes = %s,
                        updated_at = %s
                    WHERE id = %s
                      AND deleted_at IS NULL
                      AND COALESCE(provider_type, '') = ''
                    """,
                    (
                        item["provider_type"],
                        new_notes,
                        updated_at,
                        item["id"],
                    ),
                )
                updated += int(cursor.rowcount or 0)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return {
        "action": "guess-balance-provider-types",
        "dry_run": dry_run,
        "scanned": len(candidates),
        "matched": len(matched_items),
        "updated": updated,
        "items": [
            {
                "config_id": item["config_id"],
                "station_id": item["station_id"],
                "station_name": item["station_name"],
                "provider_type": item["provider_type"],
                "confidence": item["confidence"],
                "reason": item["reason"],
                "evidence": item["evidence"],
                "base_url": item["base_url"],
            }
            for item in matched_items
        ],
    }


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
                "last_check_latency_seconds": row.get("last_check_latency_seconds"),
                "recharge_ratio": row.get("recharge_ratio") or "1:1",
                "admin_notes": row.get("admin_notes") or "",
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
                    (station_id, name, website, invite_url, is_checked, checked_at, last_check_latency_seconds,
                     recharge_ratio, admin_notes, notes, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    website = VALUES(website),
                    invite_url = VALUES(invite_url),
                    is_checked = VALUES(is_checked),
                    checked_at = VALUES(checked_at),
                    last_check_latency_seconds = VALUES(last_check_latency_seconds),
                    recharge_ratio = VALUES(recharge_ratio),
                    admin_notes = VALUES(admin_notes),
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
                    station.get("last_check_latency_seconds"),
                    station.get("recharge_ratio") or "1:1",
                    station.get("admin_notes"),
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
