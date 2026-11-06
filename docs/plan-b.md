# Plan B：TrendRadar / AI News Hub

方案 A（本仓库的 RSS + LLM + schema）正常工作时，不需要方案 B。只有在以下情况才切换：

- RSS 源长期大面积不可用；
- 需要微博/知乎/抖音/B站等热榜数据，而 RSS 拿不到；
- 需要内置推送（飞书/钉钉/Telegram/邮件）；
- 不想维护抓取层，只想保留本仓库的“精炼 + 固定 schema”。

## 首选：TrendRadar

仓库：<https://github.com/sansan0/TrendRadar>  
定位：多平台热榜 + RSS 聚合 + 关键词筛选 + AI 分析 + 多渠道推送。  
License：GPL-3.0。**如果只是把它的输出转成 `raw.json` 再调用本仓库的 `refine.py`，保持两个项目独立运行即可，不要把它的代码复制进本仓库。**

### 安装方式

按官方文档选择一种：

1. **Docker**：镜像 `wantcat/trendradar`，挂载 `config/` 和 `output/`，具体端口/卷路径以官方 README 为准。
2. **本地 Python**：克隆仓库后按官方 `requirements.txt` 安装，复制 `config/config.yaml.example` 为 `config/config.yaml`。
3. **GitHub Actions**：fork 仓库，配置 secrets，按官方 workflow 运行。

### 关键配置

在 `config/config.yaml` 中：

- `rss`：加入本仓库 `config/sources.yaml` 里的 RSS 源；
- `frequency_words.txt` 或 AI 兴趣文件：加入 AI/具身/世界模型/创作者相关词；
- `notification`：按需配置飞书/钉钉/Telegram/邮件；
- `storage`：本地 SQLite 或 S3 兼容存储。

TrendRadar 的强项是“聚合 + 推送”；本仓库的强项是“固定 27 板块 + 编辑规则 + schema 校验”。两者组合，不要让两边各自生成一套周报。

### 输出映射到 raw.json

TrendRadar 的存储/导出版本会变，建议用它的 SQLite 或导出 JSON 写一个一次性转换脚本。目标结构：

```json
{
  "schema_version": 1,
  "generated_at": "2026-11-06T04:00:00Z",
  "window": {
    "days": 7,
    "since": "2026-10-30T04:00:00Z",
    "until": "2026-11-06T04:00:00Z",
    "timezone": "Asia/Shanghai"
  },
  "stats": {
    "sources_configured": 1,
    "sources_ok": 1,
    "sources_failed": 0,
    "items_fetched": 120,
    "items_after_dedupe": 120,
    "duplicates_removed": 0
  },
  "sources": [
    {
      "id": "trendradar",
      "name": "TrendRadar",
      "type": "import",
      "status": "ok",
      "fetched": 120,
      "filtered": 120,
      "kept": 120,
      "error": ""
    }
  ],
  "items": [
    {
      "id": "stable-id",
      "title": "标题",
      "url": "https://example.com/a",
      "source": "来源媒体",
      "source_id": "trendradar",
      "feed_name": "TrendRadar",
      "published_at": "2026-11-05T10:00:00Z",
      "date_confidence": "high",
      "summary": "摘要或正文前 500 字",
      "author": "",
      "lang": "zh",
      "tags": ["ai"],
      "weight": 0.9,
      "rank_score": 0.0,
      "also_seen_in": [],
      "fetched_at": "2026-11-06T04:00:00Z"
    }
  ]
}
```

最小映射：

| raw.json | TrendRadar 字段 |
| --- | --- |
| `title` | 新闻标题 |
| `url` | 原文链接 |
| `source` | 平台/媒体名 |
| `source_id` | 固定为 `trendradar` |
| `published_at` | 抓取时间或发布时间（ISO 8601） |
| `summary` | 摘要/正文；没有就留空 |
| `tags` | 平台/关键词分组 |
| `weight` | 0.8–1.0，按来源可信度 |
| `rank_score` | 0，交给 `refine.py` 重新算 |

转换后直接：

```bash
python scripts/refine.py --input output/raw.json --feedback config/feedback.yaml
python scripts/validate.py --input output/weekly-keywords.json --schema references/output-schema.json
```

`fetch.py` 不需要运行，`refine.py` 不关心数据来自 RSS 还是 TrendRadar。

## 备选：AI News Hub

“AI News Hub”不是单一官方项目，GitHub 上存在多个同名实现，能力和维护状态差异很大。选择时至少确认：

- 有明确的 RSS/Atom 抓取；
- 有去重和时间窗口；
- 有可导出的 JSON/SQLite（否则难以接入本仓库）；
- 最近 6 个月有提交；
- License 允许你的使用方式。

如果它只能生成日报文本，不能导出结构化条目，不建议作为数据层；可以继续用 TrendRadar 或本仓库的 `fetch.py`。

## 决策矩阵

| 需求 | 本仓库（A） | TrendRadar（B） |
| --- | --- | --- |
| 中英文 AI 媒体 RSS | 强 | 强 |
| 微博/知乎/抖音/B站热榜 | 弱（需 RSSHub） | 强 |
| 固定 27 板块 JSON | 强 | 需二次开发 |
| 编辑规则/事实核查 | 强 | 有 AI 分析，但偏摘要 |
| 多渠道推送 | 无 | 强 |
| 部署成本 | 低（单 Python 脚本） | 中（配置/容器/Actions） |
| License | MIT | GPL-3.0 |

## 切换原则

1. **先修 A**：单个 RSS 失败很常见，先看 `raw.json.sources` 的错误，不要因为一个源切栈。
2. **只换数据层**：尽量保留 `refine.py` + schema；这样周报结构不变。
3. **不要双跑两套周报**：两个系统各自生成会浪费 token，也会产生口径冲突。
4. **保留回溯**：无论用哪个数据层，`raw.json` 都要保留条目 id/标题/URL/时间，方便核查。
