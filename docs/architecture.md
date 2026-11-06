# Architecture

## 设计目标

1. **每周可重复**：同样的命令、同样的输出结构，方便横向对比。
2. **关键词不固化**：候选必须来自当周 `raw.json`；配置只提供信源、偏好和路由规则。
3. **可核查**：每条关键词最好能回溯到原始条目；LLM 候选必须带 evidence id。
4. **失败可解释**：单个信源失败不拖垮全局；LLM 失败有明确降级标记；schema 不通过不写正式结果。
5. **低成本运行**：抓取和去重用标准库完成，LLM 只处理筛选后的 160 条左右。

## 数据流

```text
sources.yaml
   │
   ▼
fetch.py ── HTTP ── RSS / Atom / Google News
   │                 │
   │                 ├─ 时间窗口过滤（默认 7 天）
   │                 ├─ AI 相关粗筛（ai_filter / ai_only）
   │                 ├─ 排除招聘、活动、促销
   │                 └─ 单信源条数上限
   ▼
raw.json
   │  items[]: id/title/url/source/published_at/summary/tags/weight/rank_score
   ▼
refine.py
   │
   ├─ 1. select_items()
   │     ├─ 来源权重 + 时效 + 硬数字/人物/冲突信号
   │     ├─ 为采访、创作者、具身、开源、世界模型预留名额
   │     └─ 单来源上限，避免一家媒体刷屏
   │
   ├─ 2. map：每 40 条一次，提取候选
   │     候选 = category + keyword + evidence[] + entities[] + numbers[] + tension + heat
   │
   ├─ 3. reduce：合并候选，按 schema 输出终稿
   │
   ├─ 4. audit：事实核查/版面审计
   │     只允许删除无证据条目、移动明显错放板块、合并同板块重复
   │
   └─ 5. normalize + validate
   ▼
weekly-keywords.json / .md / run-meta.json
```

## 抓取层

### 时间窗口

`--days 7` 以当前时间为终点，向前 7×24 小时。也支持 `--since/--until` 精确指定。没有发布时间的条目默认丢弃；`--include-undated` 可保留并标记 `date_confidence: unknown`。

### 解析

- 支持 RSS 2.0、Atom、RDF。
- 处理 CDATA、HTML 摘要、命名空间、相对链接、RFC 822 / ISO 8601 日期。
- Google News 的 `<source>` 作为 publisher；标题末尾的 ` - Publisher` 自动去掉。

### 粗筛

`ai_filter` 只用于减少噪声，采用“ASCII 词边界 + 中文子串”匹配，避免 `AI` 命中 `said`。`ai_only: true` 的信源跳过粗筛。

### 去重

两遍：

1. **URL 规范化**：去掉 `utm_*`、`fbclid`、`spm` 等追踪参数，统一 www/端口/末尾斜杠。
2. **标题近似**：NFKC、去标点、去来源后缀，中文按字符 bigram 计算 Jaccard；相似度 ≥ 0.90 或包含度 ≥ 0.92 视为同一事件。

合并时保留权重更高、摘要更长的条目，并把其他来源写入 `also_seen_in`。

## 选样层

`select_items()` 的分数由以下部分组成：

- `raw.rank_score` × 0.55；
- 来源权重 × 1.1；
- 时效（24h/72h/7d 三档）；
- 信号加成：采访 +0.90、创作者 +0.55、具身 +0.35、开源 +0.30、世界模型 +0.25、研究 +0.15；
- 关注名单命中；
- 标题含“重磅/炸裂/震惊”等营销词会扣分。

然后按来源上限（默认 18 条）选样，并为以下板块预留名额：

| 预留 | 数量 |
| --- | --- |
| 采访/座谈/演讲 | 12 |
| 创作者 | 18 |
| 具身智能 | 20 |
| 开源 | 12 |
| 世界模型 | 8 |

这样可以避免“纯热度排序”把大佬采访、创作者案例和小众具身信号挤掉。

## LLM 层

### Map

每 40 条一组，要求模型输出候选：

```json
{
  "category": "ai_model_and_product",
  "keyword": "某模型 2.8T 参数开放权重",
  "evidence": ["a1b2c3"],
  "entities": ["某公司"],
  "numbers": ["2.8T"],
  "tension": "开源 vs 闭源",
  "heat": 5
}
```

没有有效 evidence id 的候选会被丢弃，这是第一道防幻觉。

### Reduce

把所有候选按板块压缩到最多 12 条，连同标题清单交给终审模型。终审只能使用候选/标题中的事实，输出 schema 固定 JSON。

### Audit

可选的第三道：

- 删除证据中完全不存在的条目；
- 把 World Labs/世界模型移到 `world_models`；
- 把 VLA/具身基座模型移到 `embodied_ai_models_and_os`；
- 合并同板块完全重复；
- 不允许新增人物、数字、公司或事件；
- 如果审计删除超过 50%，保留原终稿（防止模型过度删除）。

审计结果写入 `run-meta.json` 的 `audit` 字段。

## 归一化与校验

- 去掉列表符号、序号、引号、新闻腔前缀；
- 去除空占位值（“无”“暂无”“N/A”）；
- 按 `keyword_key` 去重；
- 超过 maxLength 的条目截断到标点；
- 补齐缺失板块为 `[]`；
- 用 `references/output-schema.json` 做轻量校验（不依赖 jsonschema）。

校验失败会写 `weekly-keywords.invalid.json` 并返回退出码 4，不会静默覆盖正式结果。

## 成本

一次 565 条输入的完整跑法大约是：

- 选样 160 条；
- 4 次 map + 1 次 reduce + 1 次 audit；
- 约 7 万 prompt tokens、1 万 completion tokens（以实际 provider 计费为准）。

想省钱可以：

- `--max-items 100`；
- `--chunk-size 50`；
- `--no-audit`；
- 减少 Google News 查询；
- 用更便宜的小模型做 map，用强模型做 reduce/audit（当前版本共用同一模型，后续可扩展）。

## 扩展点

1. **加信源**：改 `config/sources.yaml`，优先 RSS；没有 RSS 用 `type: google_news`。
2. **加板块**：改 `references/output-schema.json` 的 properties/required，并在 `references/prompt.md` 补路由说明。`refine.py` 会自动读取 schema，不需要改代码。
3. **改偏好**：改 `config/feedback.yaml` 的 `editorial` / `watchlist` / `boost_terms`。
4. **换模型**：改 `.env` 的 `LLM_BASE_URL` / `LLM_MODEL`；任何 OpenAI-compatible 接口均可。
5. **换抓取层**：把 TrendRadar 或其他聚合器的输出映射成 `raw.json` 的 `items` 结构，继续复用 `refine.py`。
