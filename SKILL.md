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
- 显式录入或更新站点探测 API 配置：`scripts/site_price_registry.py upsert-probe-api`
- 删除误录入的探测 API 配置：`scripts/site_price_registry.py delete-probe-api`
- 更新站点公共信息：`scripts/site_price_registry.py update-station`
- 局部修改单条记录：`scripts/site_price_registry.py patch-record`
- 删除价格记录：`scripts/site_price_registry.py delete-records`

2. 草稿补录
- 合并当前输入到草稿：`scripts/draft_site_price.py merge`
- 查看当前草稿：`scripts/draft_site_price.py show`
- 提交草稿到正式库：`scripts/draft_site_price.py commit`
- 清空当前草稿：`scripts/draft_site_price.py clear`

3. 余额配置维护
- 新增或更新单站余额配置：`scripts/site_price_registry.py upsert-balance-config`
- 批量根据现有站点特征猜测并回填空的余额 `provider_type`：
  - `scripts/site_price_registry.py guess-balance-provider-types`

4. 站点每日新闻录入
- 明确录入站点每日新闻：`scripts/site_price_registry.py upsert-daily-news`
- 只有用户明确表达“录入新闻 / 每日新闻 / 站点新闻 / site_price_daily_news”时才执行该能力
- 普通价格录入、探测 API 配置、余额配置维护流程不自动写新闻

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

所有正式写入命令必须串行执行，禁止把多个 `upsert` / `patch-record` / `update-station` / `delete-records` / `ingest` / `batch_ingest` / `draft commit` 并行跑。写入脚本会持有统一的价格库写入锁，确保“加载全量 registry -> 修改 -> 保存全量 registry”的流程不会被另一个写入命令覆盖；如果一次要更新多个模型或分组，应按顺序逐条执行，或使用单个批量入口完成。

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

分组覆盖特别规则：
- 用户说“覆盖更新分组 / 分组倍率 / 分组备注”时，不能只更新站点级 `group_multipliers`。
- 必须同步检查并更新 `mpc_price_records` 中该站点对应分组的价格记录：旧分组不再属于当前覆盖范围时，应使用 `delete-records` 删除旧记录；新分组需要展示或计价时，应使用 `upsert` 写入对应模型价格记录、倍率和分组备注。
- `mpc_price_history` 只记录价格相关变动：充值比、倍率、模型输入/输出/缓存价格及其计算结果变化。创建记录、删除记录、分组备注、分组启用状态变化不写入价格历史。
- 分组启用状态属于价格记录字段 `is_group_enabled`；用户写“未启用/禁用”时，必须把对应 `mpc_price_records.is_group_enabled` 写为 `0`，不要只写入 `group_note`。

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

## 站点每日新闻录入

站点每日新闻写入 `site_price_daily_news`，供 `time_laravel` 展示端读取。这个能力只在用户明确要求录入新闻时使用，不参与价格草稿补录，也不随普通站点价格录入自动执行。

执行：
`scripts/site_price_registry.py upsert-daily-news --json-file <payload>`

写入模式：
- 默认新增新闻；不要按标题自动覆盖历史新闻
- 只有 payload 明确提供 `news_id` / `id` / `新闻ID` 时，才更新对应未软删记录
- 更新新闻时采用局部更新，只修改本次 payload 中出现的字段
- 误录需要隐藏时，可传 `news_id` + `status: hidden`

字段规则：
- 最小必填：`title` / `标题`
- `content` / `正文`、`source_name` / `来源名称`、链接、图片都是可选项
- `published_at` / `发布时间` 不填时默认当前北京时间
- `status` 不填默认 `published`，只允许：`draft`、`published`、`hidden`
- `is_pinned` / `是否置顶` 不填默认 `false`
- `sort_order` / `排序` 不填默认 `0`，同发布时间下越大越靠前
- `link_title` 可为空，展示端会按 `查看详情` 处理
- 非空的 `link_url`、`image_url`、扩展链接 URL、扩展图片 URL 必须是合法 `http/https`，否则脚本应报错，不静默写入
- `extra_links` / `扩展链接` 格式为数组：`[{ "title": "...", "url": "https://..." }]`
- `images` / `扩展图片` 格式为数组：`[{ "url": "https://...", "alt": "..." }]`
- 也兼容直接传 `extra_links_json` / `images_json`，但内容仍必须是 JSON 数组并通过 URL 校验

推荐 payload：
```json
{
  "title": "新闻标题",
  "content": "新闻正文",
  "published_at": "2026-05-25 15:30:00",
  "source_name": "来源名称",
  "link_title": "查看详情",
  "link_url": "https://example.com/news",
  "image_url": "https://example.com/image.png",
  "image_alt": "图片说明",
  "extra_links": [
    {"title": "相关链接", "url": "https://example.com/related"}
  ],
  "images": [
    {"url": "https://example.com/extra.png", "alt": "扩展图片"}
  ],
  "status": "published",
  "is_pinned": false,
  "sort_order": 0
}
```

更新已有新闻：
```json
{
  "news_id": 123,
  "title": "更新后的标题"
}
```

输出要求：
- 默认输出写入摘要，不扩展成新闻列表
- 至少包含：动作 `created/updated`、新闻 ID、标题、状态、发布时间、是否置顶、排序、链接/图片数量

## 探测 API 配置补充说明

- 录入站点价格时，可自动联动维护站点级 `mpc_station_probe_api_configs` 和分组级 `mpc_station_group_multipliers.api_key`
- 探测 API 配置是“站点 + 标准模型”级别，保存 `api_base_url`、endpoint 路径、`canonical_model_name`、`request_model_name`
- 分组 API Key 跟随 `mpc_station_group_multipliers` 的 `station_id + group_name`，不同站点的同名分组可以重复
- 如果没有提供 `api_base_url`，且该站点还没有任何探测 API 配置，才会默认写入一条空的 `默认API` 配置
- 如果该站点已经存在探测 API 配置，普通价格录入不再追加空的 `默认API`，避免按模型重复生成无效配置
- 一个站点通常只需要一条 `gpt-5.4` 探测 API 配置；多个分组只需要分别维护分组 API Key
- 如果此前已有空 `API Base URL` 的 `默认API`，后续显式补入同站同标准模型的真实 `API Base URL` 时，应更新这条空配置，不再新增一条重复配置
- 显式录入探测 API 配置使用：
  - `scripts/site_price_registry.py upsert-probe-api --json-file <payload>`
- 删除误录入探测 API 配置使用：
  - `scripts/site_price_registry.py delete-probe-api --json-file <payload>`
- 支持两种 payload 形式：
  - 单条：站点识别字段 + `name` + `api_base_url` + 可选 `canonical_model_name` / `request_model_name` / `api_key` / `group_name`
  - 多条兼容：站点识别字段 + `probe_apis: [{name, api_base_url, group_name?, api_key?, canonical_model_name?, request_model_name?, notes?}]`
- `group_name` 可兼容 `group`、`分组`、`价格分组`；未提供时默认 `default`
- 旧 `probe_apis` 中的 `group_name + api_key + failure_count` 只用于写入分组表；站点级探测配置不会写入 `group_name`、`api_key`、`failure_count`，也不会按分组重复创建
- 探测模型字段规则：
  - `canonical_model_name` / `标准模型名`：标准模型名，必须对应 `mpc_price_records.model_name`，例如 `gpt-5.4`
  - `request_model_name` / `请求模型名`：实际请求模型名，例如 `量gpt-5.4`
  - 若只给 `canonical_model_name`，则 `request_model_name` 默认沿用它
  - 若只给 `request_model_name`，则 `canonical_model_name` 默认沿用本次默认探测模型
  - 旧入参 `model` / `probe_model` 只作为兼容别名读取，不再写入探测配置表的 `model` 字段
- 分组 `API Key` 明文写入 `mpc_station_group_multipliers.api_key`，命令输出只能展示掩码
- 分组 `failure_count` 由重试探测维护，普通录入默认写 `0`，除非用户明确要求重置或指定
- 三种 OpenAI 风格路径默认自动写入：
  - `/v1/chat/completions`
  - `/v1/responses`
  - `/v1/responses/compact`
- 该 skill 不负责写 `mpc_station_probe_logs` 和 `mpc_station_probe_snapshots`

最小追问顺序：
1. 先确认站点身份：`站点名称 / 官网 / 别名 / station_id` 至少一个
2. 显式 probe 配置录入时，再补 `API 名称 / API Base URL`
3. 再补每个分组的 `API Key`；如果用户没有分组概念，默认写到 `default`
4. 分组 `API Key` 可为空，但空 Key 分组不会参与可请求代表价排序
5. `标准模型名` 优先沿用站点模型；如果一次录入多个模型，优先 `gpt-5.4`，否则取首个模型

统一录入模板见下方“默认录入模板”。
其中探测 API 建议直接写在同一份模板里的 `探测 API 配置` 区块，不再单独给一份分离模板。

以下文件现在不属于这个 skill 的运行时主路径，可视为历史素材、迁移数据或后续拆分参考：
- `assets/site-price-registry.json`
- `assets/site-price-history.json`
- `assets/site-price-drafts.json`
- `runtime/site-price-dashboard.html`
- `scripts/calc_model_price.py`
- `scripts/extract_model_price.py`

## 默认录入模板

以后当需要让用户补录“一个站点 + 多个分组/模型价格”时，优先输出下面这份模板，不要改成大而全的表单式问法：

```text
站点名称：qiaojiai
官网：https://model.qiaojiai.com
邀请链接：https://model.qiaojiai.com/register?aff=CEXw
充值比：1:10
是否已检测：已检测
最后一次检测延迟：1s

备注：新开站  人少
管理员备注：111111

分组/倍率/备注：
plus 0.55倍
pro20X 1.4倍

模型名称及其价格信息：
gpt-5.4 按照默认
gpt-5.5 按照默认
```

使用这份模板时遵循以下规则：

- `最后一次检测延迟` 视为站点级字段，单位按秒保存；兼容 `1s`、`1 s`、`1秒`、`1`
- `管理员备注` 视为站点级内部备注，对应字段 `admin_notes`
- `备注` 继续作为站点通用备注，对应字段 `notes`
- `是否已检测` 如果写了 `已检测`，但没写 `检测时间`，可继续沿用现有规则自动补当前时间
- `gpt-5.4 按照默认`、`gpt-5.5 按照默认` 表示该模型命中官方模型目录默认价时，可不再追问输入价/输出价
- `分组/倍率/备注` 下每行至少包含 `分组名 + 倍率`；如果没写备注，则按空备注处理
- 当用户要模板时，默认先给这份模板；只有任务明显不适合该格式时，再改用更简化的追问

统一整合版模板：
```text
站点名称：
官网：
邀请链接：
充值比：
是否已检测：
最后一次检测延迟：

备注：
管理员备注：

余额配置：
项目类型：newapi / sub2api / 自定义
余额 Base URL：
Access Token：
User ID：
启用状态：默认禁用
备注：

探测 API 配置：
1.
API 名称：默认API
API Base URL：
标准模型名：gpt-5.4
请求模型名：
启用状态：启用
备注：

分组/倍率/备注/API Key：
default 1倍 key=
plus 0.55倍 key=
pro20X 1.4倍 key=

模型名称及其价格信息：
gpt-5.4 按照默认
gpt-5.5 按照默认
```

统一整合版使用规则：
- 站点首次录入且不存在余额配置时，技能会自动插入一条 `mpc_station_balance_configs`
- 站点已存在余额配置时，如果本次输入显式包含 `项目类型`、`余额 Base URL`、`Access Token`、`User ID`、`启用状态`、`余额备注` 等余额字段，应更新已有配置，不再跳过
- `项目类型` 支持：`newapi`、`sub2api`、`自定义`
- `自定义` 会映射为 `custom_json_path`
- `余额 Base URL` 默认取官网；如果不填且官网为空，则会写空值，后续需手补
- `Access Token`、`User ID` 可录入；输出摘要只展示 `Access Token` 掩码
- `sub2api` 或自定义余额请求需要 API Key 时，不在余额配置里单独填写；余额探测会使用 `分组/倍率/备注/API Key` 中当前最便宜可用分组的 Key
- 余额配置默认不启用；路径类字段本次不在模板中展开，默认留空，后续按需再补
- 若批量初始化后仍存在空的 `provider_type`，可再执行 `guess-balance-provider-types`
- 该批量猜测只吃高置信度信号，优先参考：
  - `mpc_station_probe_logs.response_summary`
  - `mpc_station_probe_snapshots.response_summary`
  - `request_url / 官网 / 探测 API 根地址` 中的 `New API / Sub2API / newapi / newcli / sub2api`
- `探测 API 配置` 通常只写一组站点级配置；同站点不同分组共享同一 API Base URL 和探测模型
- 每组自己的 API Key 写在 `分组/倍率/备注/API Key` 中；比如 `plus 0.55倍 key=sk-xxx`
- 不同站点可以都使用 `default`、`plus` 等相同分组名；分组 Key 按 `station_id + group_name` 匹配，不会互相影响
- `标准模型名` 用于展示和模型一致性评分，例如 `gpt-5.4`
- `请求模型名` 用于真实发起探测请求；如果不填，默认沿用 `标准模型名`
- 如果完全不填写 `探测 API 配置`，且站点还没有探测 API 配置，技能会自动补一条空的 `默认API` 记录；已有探测配置时跳过自动补空配置
- `API Base URL` 只填根地址，例如 `https://api.example.com`
- 默认不需要单独填写三种 OpenAI 风格路径：
  - `/v1/chat/completions`
  - `/v1/responses`
  - `/v1/responses/compact`

每日新闻录入模板：
```text
新闻标题：
新闻正文：
发布时间：
来源名称：

主链接标题：
主链接：

主图地址：
主图说明：

扩展链接：
1. 标题：  URL：
2. 标题：  URL：

扩展图片：
1. URL：  说明：
2. URL：  说明：

状态：published
是否置顶：否
排序：0
```

每日新闻模板使用规则：
- 只有用户明确要求录入新闻时才给这份模板；用户只是要录价格模板时，仍使用上面的站点价格统一整合版模板
- 如果用户只给标题，也可以写入一条默认 `published` 新闻；缺少标题时再追问标题
- 如果用户给了链接或图片地址，必须能通过 `http/https` 校验，否则让用户修正后再写入
