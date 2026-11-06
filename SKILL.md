---
name: weekly-ai-keyword-radar
description: 每周AI与具身智能关键词提炼。当用户说“这周AI热点关键词”“生成周报JSON”“跑一下关键词雷达”时触发。抓取过去7天的信源，按用户定义的结构输出关键词JSON，包含“大佬采访座谈分享”板块。
metadata:
  version: 1.0.0
  status: active
  repository: https://github.com/garycaogan/weekly-ai-keyword-radar
  license: MIT
---

# Weekly AI Keyword Radar

**目的**：每周从公开信源里提炼一组可复用的 AI 与具身智能关键词，供选题、趋势判断和内容创作使用。关键词必须来自当周材料，不是固定词表。

## 何时使用

- 用户说“这周 AI 热点关键词”“跑一下关键词雷达”“生成周报 JSON”“看看本周具身智能/世界模型/AI 创作有什么”。
- 需要一份结构固定、可以每周对比的 JSON。
- 需要包含“大佬采访座谈分享”板块，且不接受用普通新闻凑数。

不适用：需要完整新闻摘要、需要逐条翻译、需要实时股价/融资数据库、需要抓取付费墙内容。

## 三层结构

1. **数据层**：`scripts/fetch.py` 按时间窗口抓取 RSS/Atom，做 AI 相关粗筛、URL/标题去重，写入 `output/raw.json`。
2. **智能层**：`scripts/refine.py` 先按来源权重、时效、人物/数字/冲突信号选样，再调用 LLM 做分块候选提取 + 终审合并 + 事实核查，写入 `output/weekly-keywords.json`。
3. **复用层**：`references/output-schema.json` 固定 27 个板块的结构；`SKILL.md` 固定调用方式和验收标准。

关键词每周实时更新。`config/feedback.yaml` 只记录偏好和路由规则，不是关键词白名单。

## 快速开始

```bash
cd weekly-ai-keyword-radar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 OpenAI-compatible 的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
```

## 执行流程

按顺序运行，不要跳过抓取直接编造：

```bash
python scripts/fetch.py --days 7
python scripts/refine.py --input output/raw.json --feedback config/feedback.yaml
```

等价的单命令：

```bash
python scripts/run_weekly.py --days 7 --archive --fallback-on-error
```

校验：

```bash
python scripts/validate.py --input output/weekly-keywords.json --schema references/output-schema.json
```

最后读取 `output/weekly-keywords.json`，把**完整 JSON**（不要省略空数组、不要只给摘要）放在 ```json 代码块里输出给用户。如果用户要求文件，直接给路径；如果用户要求“贴出来”，原样粘贴。

## 输出规范

严格遵循 `references/output-schema.json`。27 个顶层字段全部存在，每个值都是字符串数组。没有合格条目的板块返回 `[]`，不要凑数。

重点板块：

- `ai_model_and_product`：模型/产品发布与能力。
- `ai_agent_and_governance`：Agent 产品、协议、治理。
- `ai_infrastructure_and_business`：算力、芯片、数据中心、融资并购、商业化。
- `ai_regulation_and_safety`：法律、监管、标准、安全事件。
- `company_event_keywords`：公司级 IPO、并购、合作、组织、诉讼。
- `embodied_ai_*`：具身智能的指标、模型/OS、硬件/部署、融资、标准政策。
- `ai_creation_and_workflow`：AI 视频/图像/音乐/短剧的生产流程。
- `ai_open_source`：开源模型、权重、代码、数据集、社区数据。
- `world_models`：世界模型、空间智能、物理仿真。
- `3d_game_ai`：3D 资产、游戏引擎、UE/Unity、Blender/CAD。
- `ai_tools_and_tech_life`：消费级 AI 工具、硬件、数字生活。
- `reveal_and_expose`：调查、爆料、泄露、争议、处罚。
- `disruption`：行业/职业/组织被重构。
- `tech_shock`：强冲击力的技术演示、首次实现、极限数字。
- `*_博主自创`：创作者侧案例，必须有平台/作品/数据/方法。
- `viral_lexicon`：本周传播热词。
- `creator_viral_lexicon`：创作者圈层黑话/方法论词。
- `executive_interviews_and_talks`：大佬采访、座谈、播客、演讲、问答、内部信。**没有合格条目必须返回 `[]`。**

## 编辑规则

1. 只使用输入材料中的事实，不补全、不调用记忆、不编造。
2. 优先具体人物、公司、模型、产品、场景；优先硬数字；优先对抗性张力。
3. 一条只讲一件事；同一事件不要按来源重复。
4. 不写新闻标题；去掉“发布/宣布/表示/据悉”等新闻腔。
5. 不用“重磅/炸裂/刷屏/改变一切”等空词（这些词只可能出现在 `viral_lexicon`）。
6. 大佬采访必须有具体人物 + 具体场合 + 可复述观点；普通发布会/通稿不放。
7. 创作者板块只收个人/小团队用 AI 做出东西的案例，不收公司通稿。
8. 没有合格条目就留空；空数组是合格输出。

## 配置

- `config/sources.yaml`：信源、权重、AI 粗筛词、Google News 查询。
- `config/feedback.yaml`：编辑偏好、路由规则、人物/公司关注列表、来源权重覆盖。
- `references/prompt.md`：编辑手册（LLM 的系统提示词底座）。
- `references/output-schema.json`：固定输出结构。
- `.env`：LLM 配置，不要提交到 Git。

新增信源时优先 RSS/Atom；没有 RSS 的站点用 Google News 查询兜底。公共 RSSHub 经常超时，不建议默认启用；自建后再打开 `config/sources.yaml` 末尾的可选信源。

## 验收清单

- `output/raw.json` 的 `window` 覆盖最近 7 天，`items` 不为空。
- `output/weekly-keywords.json` 通过 `validate.py`。
- 27 个字段齐全；每个字符串非空；没有重复。
- 至少检查 5 条关键词能否在 `raw.json` 找到对应事实。
- `executive_interviews_and_talks` 每条都有人物和场合；没有就为空。
- 输出给用户的是完整 JSON，不是截断版。

## 失败与降级

- **没有 LLM key**：不要伪造 LLM 结果。设置 key，或显式使用 `--no-llm` 生成抽取式兜底，并在结果里说明“heuristic fallback”。
- **LLM 超时/限流**：`--fallback-on-error` 会降级并写 `run-meta.json`；不要静默失败。
- **某个 RSS 失败**：抓取层会继续跑；`raw.json.sources` 里有每个信源的状态。单个信源失败不影响整体。
- **抓取为 0**：检查网络、时间窗口、信源可用性；不要用旧闻或记忆补。
- **输出校验失败**：看 `output/weekly-keywords.invalid.json`，修提示词/归一化后重跑；不要手改 schema 绕过。

## 方案 B（Plan A 失败时）

如果 RSS 抓取长期不可用，或用户明确要求换栈：

1. 优先安装 `TrendRadar`（`sansan0/TrendRadar`）：聚合热榜 + RSS + 关键词筛选 + 推送，Docker/GitHub Actions 均可。
2. 备选 `AI News Hub`：RSS 聚合 + 兴趣评分 + 日报/周报；不同实现差异较大，先确认仓库再装。
3. 把 TrendRadar 的输出转成 `raw.json` 的 `items` 结构，再复用本仓库的 `refine.py` 和 schema；这样输出结构不变。
4. 迁移步骤和字段映射见 `docs/plan-b.md`。

方案 A 正常工作时不要提前安装方案 B，避免两套抓取重复占资源。

## 文件地图

```text
weekly-ai-keyword-radar/
├── SKILL.md
├── README.md
├── scripts/
│   ├── fetch.py          # 第一层：抓取 + 去重
│   ├── refine.py         # 第二层：LLM 提炼 + 审计
│   ├── validate.py       # schema 校验
│   └── run_weekly.py     # 一键流水线
├── config/
│   ├── sources.yaml
│   └── feedback.yaml
├── references/
│   ├── output-schema.json
│   └── prompt.md
└── output/
    ├── raw.json
    ├── weekly-keywords.json
    └── weekly-keywords.md
```
