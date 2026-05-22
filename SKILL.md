---
name: model-price-calculator
description: 维护中转站价格库的 MySQL 写入技能，支持新增或覆盖价格记录、更新站点信息、局部修改单条记录、删除记录，以及基于 MySQL 草稿表的多轮补录。
---

# Model Price Writer

这个 skill 现在只负责“写库”，不再负责查询、排行、历史查看、HTML 展示或本地页面刷新。

事实来源只有 MySQL：
- 正式站点与价格记录写入 MySQL 正式表
- 多轮补录草稿写入 MySQL 草稿表
- 线上展示页已经独立实时读取 MySQL，不属于这个 skill 的职责
- 所有写入时间字段统一按北京时间（UTC+8）处理，包括创建时间、更新时间、检测时间、验证时间、历史变更时间和草稿时间

## 能力范围

保留两类能力：

1. 正式写入
- 新增或覆盖某站某模型某分组价格：`scripts/site_price_registry.py upsert`
- 更新站点公共信息：`scripts/site_price_registry.py update-station`
- 局部修改单条记录：`scripts/site_price_registry.py patch-record`
- 删除价格记录：`scripts/site_price_registry.py delete-records`

2. 草稿补录
- 合并当前输入到草稿：`scripts/draft_site_price.py merge`
- 查看当前草稿：`scripts/draft_site_price.py show`
- 提交草稿到正式库：`scripts/draft_site_price.py commit`
- 清空当前草稿：`scripts/draft_site_price.py clear`

不再承诺以下能力：
- 查询、搜索、排行、最便宜站点、TopN
- 历史查看
- HTML 页面、仪表盘、缓存刷新
- 本地 JSON 运行时存储
- 独立的价格分析/展示文案输出

## 默认工作方式

1. 先判断用户是在做“正式写入”还是“信息还不完整，需要补录进草稿”。

2. 如果信息完整，直接正式写入。
正式写入最少需要：
- 站点识别信息：别名 / 站点名称 / 官网 三者之一
- 模型名称
- 输入价格或该模型可命中官方默认价
- 输出价格或该模型可命中官方默认价

3. 如果信息不完整，优先合并到草稿，再补问最小必要字段。

4. 每次追问只问当前写入最缺的 1 到 2 个字段，不要把 skill 用成大表单。

## 字段规则

- `模型名称` 默认 `gpt5.4`
- `分组` 缺省可沿用同站同模型最近一次记录；没有历史时回退为 `default`
- `倍率` 缺省可沿用同站同模型最近一次记录；没有历史时回退为 `1`
- `充值比` 是站点级字段，缺省为 `1:1`
- `输入价格`、`输出价格` 在以下两种情况满足其一即可：
  - 用户明确提供价格
  - 模型命中官方模型目录，允许自动补默认输入/输出价格
- `缓存读取价格`、`缓存创建价格`、`分组备注`、`售价`、`邀请链接`、`备注`、`是否已检测`、`检测时间` 都是可选项
- 如果模型命中官方模型目录，脚本可自动补默认输入价、输出价和缓存读取价；这个 skill 不再把“价格计算结果展示”作为主要输出

站点检测字段规则：
- `是否已检测` 可写：`已检测 / 未检测 / 是 / 否 / true / false / 1 / 0`
- 用户明确写了 `已检测` 时，如未给 `检测时间`，默认自动补当前时间

批量文本输入补充规则：
- 支持先给站点字段块，再给分组说明，再给模型价格段，例如：
  - `站点名称：`
  - `官网：`
  - `邀请链接：`
  - `充值比：`
  - `是否已检测：`
  - `备注：`
- 支持一行写多个分组配置，格式可为：`分组/倍率/备注：Codex-lite分组 0.4倍(备注...) Codex-Max分组 0.7倍(备注...)`
- 若 `充值比` 写成纯数值，如 `充值比：1.3`，默认按 `1:1.3` 理解；若明确写成 `1:1.3`，则按原值使用
- 若用户明确说明“这是计算倍率后的价格”或“无需计算倍率”，优先把该语义视为已声明，不额外追问是否还要再按倍率换算
- 若用户没有明确说明价格是否已计算倍率，则可以结合上下文继续判断；不要默认强行重算

## 引导式补录

如果用户信息不全，不要一次性抛长表单，按下面顺序补：

1. 先拿到站点身份
- 至少一个：`站点别名`、`站点名称`、`官网`

2. 再拿到价格归属
- 至少一个：`模型名称`
- 如果存在多个档位，再补 `分组`

3. 最后补写入必需价格
- `输入价格`
- `输出价格`

如果模型已经命中官方模型目录，且用户明确表示“按默认价格”，则可以不再追问输入/输出价格，直接按官方默认输入、输出、缓存读取价写入。

适合的短追问示例：
- `先给我一个能识别这个站的信息：站点别名、站点名称、或官网地址，三选一就行。`
- `这个站这次要写哪个模型？如果有分组也可以一起发我。`
- `还缺输入和输出价格，直接给数值也行，比如 $2.5 / $20。`

## 草稿补录规则

草稿是 MySQL 持久化会话态，不是本地 JSON 草稿。

当信息不足以正式写入时：
- 先执行：`scripts/draft_site_price.py merge --json-file <payload>`
- 然后根据返回的 `missing_fields` 和 `next_questions` 继续补问

如果草稿里已经有模型名，且该模型能命中官方模型目录：
- 可以直接按默认输入价、输出价、缓存读取价视为已满足价格条件
- 用户明确说“按默认价格”时，可直接继续 commit，不必再追问输入/输出价

用户要求查看当前补录状态时：
- 执行：`scripts/draft_site_price.py show --json-file <payload>`

草稿满足正式写入条件后：
- 执行：`scripts/draft_site_price.py commit --json-file <payload>`

用户放弃当前补录时：
- 执行：`scripts/draft_site_price.py clear --json-file <payload>`

`clear` 只清草稿，不影响正式库。

## 正式写入规则

### 1. 新增或覆盖价格

适用场景：
- 新增一个站点价格
- 给同一站新增模型
- 给同一模型新增分组
- 用最新价格覆盖同站点 + 同模型 + 同分组记录

执行：
`scripts/site_price_registry.py upsert --json-file <payload>`

### 2. 更新站点字段

适用场景：
- 改官网
- 改邀请链接
- 改备注
- 改站点级充值比
- 改检测状态

执行：
`scripts/site_price_registry.py update-station --json-file <payload>`

### 3. 局部修改单条记录

适用场景：
- 只改输入价
- 只改输出价
- 只改倍率
- 只改缓存读取价
- 只改分组备注

执行：
`scripts/site_price_registry.py patch-record --json-file <payload>`

### 4. 删除记录

适用场景：
- 删除某站某模型某分组记录
- 删除某站某模型下的一批记录

执行：
`scripts/site_price_registry.py delete-records --json-file <payload>`

## 批量与统一入库

如果用户一次给出的是“一个站点 + 一段原始价格文本”，优先走：
`scripts/ingest_site_price.py`

如果用户一次给出的是“一个站点 + 多个模型 / 多个分组 / 多段倍率价格块”，优先走：
`scripts/batch_ingest_site_price.py`

这两个入口现在只负责把输入整理后写入 MySQL，不再负责排行、站点展示或 HTML 刷新。

对于这类原始块文本，优先按下面的理解顺序处理：
1. 先识别站点公共字段：`站点名称 / 官网 / 邀请链接 / 充值比 / 是否已检测 / 备注`
2. 再识别分组信息：`分组/倍率/备注`
3. 最后识别模型价格字段：`输入价格 / 补全价格(输出价格) / 缓存读取价格 / 缓存创建价格`
4. 如果用户明确说“以下价格已经是计算倍率后的”，默认不要再反复追问“是否还需要计算倍率”；只有当用户没说明时，才继续判断是否需要按倍率处理

## 输出要求

默认输出使用“写入结果摘要”，不要再返回排行榜、站点 Markdown 展示块或 HTML。

至少包含：
- 命中的站点
- 本次动作类型
- 本次变更字段
- 影响的记录
- 草稿状态或提交结果

如果脚本返回的是 JSON 对象：
- 优先整理成简洁中文摘要
- 不要再扩展成展示型榜单或完整站点列表

## 参考

如果只是做这个写库 skill 的维护，优先使用：
- `scripts/site_price_registry.py`
- `scripts/draft_site_price.py`
- `scripts/ingest_site_price.py`
- `scripts/batch_ingest_site_price.py`

以下文件现在不属于这个 skill 的运行时主路径，可视为历史素材、迁移数据或后续拆分参考：
- `assets/site-price-registry.json`
- `assets/site-price-history.json`
- `assets/site-price-drafts.json`
- `runtime/site-price-dashboard.html`
- `scripts/calc_model_price.py`
- `scripts/extract_model_price.py`
