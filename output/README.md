# output

| 文件 | 说明 | 是否提交 |
| --- | --- | --- |
| `raw.json` | 抓取、过滤、去重后的原始条目 | 否（Actions artifact） |
| `candidates.json` | LLM map 阶段提取的候选与 evidence | 否 |
| `run-meta.json` | 本次运行的模式、模型、token、审计状态 | 否 |
| `weekly-keywords.json` | 固定 27 板块的最终 JSON | 是 |
| `weekly-keywords.md` | 人读版，按板块列条目 | 是 |
| `archive/YYYY-Www.json` | 每周归档 | 是 |
| `prompts/` | `--dry-run` 生成的提示词 | 否 |

`weekly-keywords.json` 是唯一权威结果；`raw.json` 用于事实核查，`run-meta.json` 用于排查模型/审计问题。
