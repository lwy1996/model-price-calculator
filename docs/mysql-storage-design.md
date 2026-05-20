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
