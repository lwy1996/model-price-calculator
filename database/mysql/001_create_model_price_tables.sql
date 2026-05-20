-- model-price-calculator MySQL 5.6 schema
SET NAMES utf8mb4;

-- 说明：
-- 1. MySQL 5.6 不支持原生 JSON 类型，JSON 结构统一用 LONGTEXT 保存。
-- 2. 业务删除统一使用 deleted_at 软删除。
-- 3. MySQL 5.6 不支持部分唯一索引，delete_token 用于实现“未删除数据唯一、软删除后可重建”。
-- 4. 软删除时建议同时执行：
--    UPDATE 表名 SET deleted_at = NOW(), delete_token = CONCAT('deleted:', id) WHERE id = ? AND deleted_at IS NULL

CREATE TABLE IF NOT EXISTS `mpc_stations` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `station_id` VARCHAR(191) NOT NULL COMMENT '站点业务ID，脚本内部稳定引用',
  `name` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '站点名称',
  `website` VARCHAR(500) NOT NULL DEFAULT '' COMMENT '官网地址',
  `recharge_ratio` VARCHAR(64) NOT NULL DEFAULT '1:1' COMMENT '充值比，例如 1:1、1:6、29.99:1000',
  `notes` TEXT NULL COMMENT '站点备注',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '最后更新时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  `delete_token` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '软删除唯一键占位；未删除为空，删除时写入唯一值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_station_id_active` (`station_id`, `delete_token`),
  KEY `idx_station_name` (`name`),
  KEY `idx_station_website` (`website`(191)),
  KEY `idx_station_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型价格站点表';

CREATE TABLE IF NOT EXISTS `mpc_station_aliases` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `station_id` VARCHAR(191) NOT NULL COMMENT '站点业务ID',
  `alias` VARCHAR(191) NOT NULL COMMENT '站点别名、简称或可用于检索的名称',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '最后更新时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  `delete_token` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '软删除唯一键占位；未删除为空，删除时写入唯一值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_alias_active` (`alias`, `delete_token`),
  KEY `idx_alias_station_id` (`station_id`),
  KEY `idx_alias_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型价格站点别名表';

CREATE TABLE IF NOT EXISTS `mpc_station_group_multipliers` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `station_id` VARCHAR(191) NOT NULL COMMENT '站点业务ID',
  `group_name` VARCHAR(191) NOT NULL COMMENT '站点分组名称，例如 default、vip、pro',
  `multiplier` DECIMAL(18,6) NOT NULL COMMENT '该分组默认倍率',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '最后更新时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  `delete_token` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '软删除唯一键占位；未删除为空，删除时写入唯一值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_station_group_active` (`station_id`, `group_name`, `delete_token`),
  KEY `idx_group_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型价格站点分组倍率表';

CREATE TABLE IF NOT EXISTS `mpc_price_records` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `record_id` VARCHAR(191) NOT NULL COMMENT '价格记录业务ID，通常由站点ID、模型、分组组成',
  `station_id` VARCHAR(191) NOT NULL COMMENT '站点业务ID',
  `model_name` VARCHAR(191) NOT NULL COMMENT '规范化后的模型名称',
  `group_name` VARCHAR(191) NOT NULL DEFAULT 'default' COMMENT '价格所属分组',
  `group_note` TEXT NULL COMMENT '分组备注，例如号池、限制、售后说明',
  `source` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '价格来源，例如 manual、official、screenshot',
  `currency_hint` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '原始货币提示',
  `input_price` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '输入价格原始文本',
  `output_price` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '输出价格原始文本',
  `cache_price` VARCHAR(191) DEFAULT NULL COMMENT '缓存价格原始文本，兼容旧字段',
  `cache_read_price` VARCHAR(191) DEFAULT NULL COMMENT '缓存读取价格原始文本',
  `cache_write_price` VARCHAR(191) DEFAULT NULL COMMENT '缓存创建价格原始文本',
  `multiplier` DECIMAL(18,6) NOT NULL DEFAULT 1.000000 COMMENT '倍率',
  `recharge_ratio` VARCHAR(64) NOT NULL DEFAULT '1:1' COMMENT '记录当时使用的充值比',
  `sale_price` VARCHAR(191) DEFAULT NULL COMMENT '销售价原始文本，用于利润分析',
  `tags_json` LONGTEXT NULL COMMENT '标签数组 JSON 字符串',
  `computed_json` LONGTEXT NULL COMMENT '计算结果 JSON 字符串，兼容 MySQL 5.6',
  `input_rmb_per_m` DECIMAL(18,6) DEFAULT NULL COMMENT '输入人民币每百万 tokens 折算价',
  `output_rmb_per_m` DECIMAL(18,6) DEFAULT NULL COMMENT '输出人民币每百万 tokens 折算价',
  `cache_read_rmb_per_m` DECIMAL(18,6) DEFAULT NULL COMMENT '缓存读取人民币每百万 tokens 折算价',
  `summary_rmb_per_m` DECIMAL(18,6) DEFAULT NULL COMMENT '综合人民币每百万 tokens 折算价，用于排行',
  `last_verified_at` DATETIME NULL COMMENT '最后验证时间',
  `expires_at` DATETIME NULL COMMENT '明确过期时间',
  `stale_after_days` INT UNSIGNED DEFAULT NULL COMMENT '多少天未验证后视为可能过期',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '最后更新时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  `delete_token` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '软删除唯一键占位；未删除为空，删除时写入唯一值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_record_id_active` (`record_id`, `delete_token`),
  UNIQUE KEY `uk_station_model_group_active` (`station_id`, `model_name`, `group_name`, `delete_token`),
  KEY `idx_price_station` (`station_id`),
  KEY `idx_price_model_group` (`model_name`, `group_name`),
  KEY `idx_price_summary` (`summary_rmb_per_m`),
  KEY `idx_price_updated_at` (`updated_at`),
  KEY `idx_price_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型价格记录表';

CREATE TABLE IF NOT EXISTS `mpc_price_history` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `changed_at` DATETIME NOT NULL COMMENT '变更发生时间',
  `source` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '变更来源，例如 upsert、patch-record、station_recharge_ratio_changed',
  `record_id` VARCHAR(191) NOT NULL COMMENT '价格记录业务ID',
  `station_id` VARCHAR(191) NOT NULL COMMENT '站点业务ID',
  `model_name` VARCHAR(191) NOT NULL COMMENT '模型名称',
  `group_name` VARCHAR(191) NOT NULL DEFAULT 'default' COMMENT '分组名称',
  `changed_fields_json` LONGTEXT NULL COMMENT '变更字段数组 JSON 字符串',
  `old_json` LONGTEXT NULL COMMENT '变更前价格快照 JSON 字符串',
  `new_json` LONGTEXT NULL COMMENT '变更后价格快照 JSON 字符串',
  `summary_change_percent` DECIMAL(18,6) DEFAULT NULL COMMENT '综合价格变化百分比',
  `created_at` DATETIME NOT NULL COMMENT '记录创建时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  PRIMARY KEY (`id`),
  KEY `idx_history_record` (`record_id`),
  KEY `idx_history_station_model` (`station_id`, `model_name`, `group_name`),
  KEY `idx_history_changed_at` (`changed_at`),
  KEY `idx_history_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型价格变更历史表';

CREATE TABLE IF NOT EXISTS `mpc_drafts` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `draft_id` VARCHAR(191) NOT NULL COMMENT '草稿业务ID，默认由站点信息推导',
  `station_json` LONGTEXT NULL COMMENT '草稿中的站点信息 JSON 字符串',
  `pricing_json` LONGTEXT NULL COMMENT '草稿中的价格信息 JSON 字符串',
  `raw_text` LONGTEXT NULL COMMENT '用户粘贴的原始文本',
  `notes` TEXT NULL COMMENT '草稿备注',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '最后更新时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  `delete_token` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '软删除唯一键占位；未删除为空，删除时写入唯一值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_draft_active` (`draft_id`, `delete_token`),
  KEY `idx_draft_updated_at` (`updated_at`),
  KEY `idx_draft_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型价格多轮会话草稿表';

CREATE TABLE IF NOT EXISTS `mpc_models` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `canonical` VARCHAR(191) NOT NULL COMMENT '模型规范名称',
  `default_input_price` VARCHAR(191) DEFAULT NULL COMMENT '官方默认输入价格',
  `default_output_price` VARCHAR(191) DEFAULT NULL COMMENT '官方默认输出价格',
  `default_cache_read_price` VARCHAR(191) DEFAULT NULL COMMENT '官方默认缓存读取价格',
  `default_cache_write_price` VARCHAR(191) DEFAULT NULL COMMENT '官方默认缓存创建价格',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '最后更新时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  `delete_token` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '软删除唯一键占位；未删除为空，删除时写入唯一值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_model_active` (`canonical`, `delete_token`),
  KEY `idx_model_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型目录表';

CREATE TABLE IF NOT EXISTS `mpc_model_aliases` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `canonical` VARCHAR(191) NOT NULL COMMENT '模型规范名称',
  `alias` VARCHAR(191) NOT NULL COMMENT '模型别名',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '最后更新时间',
  `deleted_at` DATETIME NULL COMMENT '软删除时间，NULL 表示未删除',
  `delete_token` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '软删除唯一键占位；未删除为空，删除时写入唯一值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_model_alias_active` (`alias`, `delete_token`),
  KEY `idx_model_alias_canonical` (`canonical`),
  KEY `idx_model_alias_deleted_at` (`deleted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型别名表';

CREATE TABLE IF NOT EXISTS `mpc_schema_migrations` (
  `id` INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `version` VARCHAR(191) NOT NULL COMMENT '迁移版本号',
  `description` VARCHAR(500) NOT NULL DEFAULT '' COMMENT '迁移说明',
  `executed_at` DATETIME NOT NULL COMMENT '执行时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_schema_version` (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型价格库数据库迁移记录表';
