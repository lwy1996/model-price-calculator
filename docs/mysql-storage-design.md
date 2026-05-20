# MySQL 存储设计说明

## 目标

`model-price-calculator` 当前以 `assets/*.json` 作为价格库、历史、草稿和模型目录的事实来源。后续迁移到 MySQL 时，数据库应成为长期事实来源，JSON 文件仅作为导入来源、备份导出或兼容产物。

建表脚本位于：

```text
database/mysql/001_create_model_price_tables.sql
```

如果数据库客户端复制执行带中文字段注释的版本时报 `1064`，可先执行无中文注释的兼容版本排查客户端编码或注释解析问题：

```text
database/mysql/001_create_model_price_tables.compat.sql
```

## 设计约束

- 兼容 MySQL 5.6。
- 不使用 MySQL 原生 JSON 类型，复杂结构用 `LONGTEXT` 保存 JSON 字符串。
- 不在站点表记录 API 地址。
- 不记录测试数据标识。
- 不维护价格可信度字段。
- 所有业务删除统一使用 `deleted_at` 软删除。
- 查询、排行、导入校验默认只处理 `deleted_at IS NULL` 的记录。

## 软删除约定

MySQL 5.6 不支持部分唯一索引，无法直接表达“仅未删除记录唯一”。因此表中使用 `delete_token` 辅助唯一约束：

- 未删除记录：`deleted_at IS NULL` 且 `delete_token = ''`
- 已删除记录：`deleted_at` 写入删除时间，`delete_token` 写入唯一值

推荐软删除语句：

```sql
UPDATE `mpc_price_records`
SET `deleted_at` = NOW(),
    `delete_token` = CONCAT('deleted:', `id`)
WHERE `id` = ?
  AND `deleted_at` IS NULL;
```

其他带 `delete_token` 的表同理。

## 表职责

- `mpc_stations`：站点主信息。
- `mpc_station_aliases`：站点别名，用于搜索和去重。
- `mpc_station_group_multipliers`：站点分组默认倍率。
- `mpc_price_records`：模型价格记录和排行冗余字段。
- `mpc_price_history`：价格变更历史。
- `mpc_drafts`：多轮录入草稿。
- `mpc_models`：模型规范名和官方默认价格。
- `mpc_model_aliases`：模型别名。
- `mpc_schema_migrations`：数据库迁移版本记录。

## 后续接入建议

迁移脚本接入时建议分三步：

1. 从现有 JSON 只导入 MySQL，不改变现有读写路径。
2. 对比 MySQL 导出结果与 JSON 结构，确认站点数、价格记录数、历史数、草稿数、模型数一致。
3. 再将脚本存储层切换为 MySQL 主读写，并保留 JSON 导出命令作为备份。

`mysql-mcp` 适合用于 Codex 做只读核对、报告和排障；Python 脚本运行时应使用常规 MySQL 驱动连接数据库，不应依赖 MCP 作为业务运行时。

## 启用 MySQL 存储

脚本默认继续使用本地 JSON。推荐复制模板创建本地配置：

```powershell
Copy-Item config/storage.example.json config/storage.local.json
```

然后编辑 `config/storage.local.json`：

```json
{
  "backend": "mysql",
  "mysql": {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "your_user",
    "password": "your_password",
    "database": "mpc_station"
  }
}
```

`config/storage.local.json` 已加入 `.gitignore`，可以存放本机真实连接信息，不应提交。

如果需要临时覆盖配置，也可以继续使用环境变量：

```powershell
$env:MPC_STORAGE_BACKEND = "mysql"
$env:MPC_STORAGE_CONFIG = "F:\path\to\storage.local.json"
```

环境变量优先级高于配置文件；读取顺序是 `MPC_STORAGE_CONFIG` 指定文件、`config/storage.local.json`、`config/storage.example.json`。

已接入 MySQL 后端的脚本入口：

- `scripts/site_price_registry.py`
- `scripts/draft_site_price.py`
- `scripts/model_catalog.py`

MySQL 后端使用 `scripts/mysql_storage.py` 做适配层。现有业务逻辑仍使用旧的 registry/draft/catalog 字典结构，适配层负责 MySQL 表与旧结构之间的装配和回写。

如需从 MySQL 导出旧 JSON 结构备份，可在配置好上述环境变量后运行：

```powershell
python scripts/export_mysql_json.py
```

默认导出到：

```text
runtime/mysql-json-export/
```

## 当前 JSON 数据同步方式

先执行建表脚本，再从现有 JSON 生成导入 SQL：

```powershell
python scripts/export_mysql_seed.py
```

默认输出：

```text
runtime/mysql-seed-current-data.sql
```

这个脚本只读取现有 JSON，并生成可审查的 SQL 文件，不会连接或修改 MySQL。导入 SQL 会迁移：

- `assets/site-price-registry.json` 中的站点、别名、分组倍率和价格记录
- `assets/site-price-history.json` 中的价格变更历史
- `assets/site-price-drafts.json` 中的草稿
- `assets/model-catalog.json` 中的模型目录和模型别名

导入时会按当前 MySQL 表结构主动忽略旧 JSON 中不再入库的字段，例如 `api_base_url`、`is_test_data`、`confidence_score`。

建议执行顺序：

1. 执行 `database/mysql/001_create_model_price_tables.sql`。
2. 执行 `python scripts/export_mysql_seed.py`。
3. 打开并审查 `runtime/mysql-seed-current-data.sql`。
4. 在目标数据库执行 `runtime/mysql-seed-current-data.sql`。
5. 执行只读核对 SQL，例如统计 `mpc_stations`、`mpc_price_records`、`mpc_price_history`、`mpc_models` 的数量。
