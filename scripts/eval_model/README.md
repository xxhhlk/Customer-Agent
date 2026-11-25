# 模型回复正确率评测

用历史会话里**人工客服的真实回复**作金标准，横向比较各个 LLM 在同一批买家问题上的表现。

## 口径（A′）

| 组成 | 来源 |
|---|---|
| system prompt | `config.json` 的 `prompt.description` + `instructions` + 禁用词块 + `additional_context`（与生产一致） |
| 会话历史 | `chat_message_records` 里同 `(shop_id, buyer_uid)` 且在该条人工回复之前的消息，压到 ~2400 token |
| 本轮输入 | 买家消息聚合 + `[人工客服已回复]` 历史块（最近 20 条）+ `[参考话术]`（知识库 143 条里 IDF 检索 top-3） |
| 金标准 | 该条 `reply_source='staff'` 的原文 |

不挂工具、不检索 LanceDB、不注入订单/素材清单 —— 单次生成，保证各模型输入完全一致。

## 用法

```powershell
# 1. 建样本（分层抽样，固定 seed 可复现）
python -m scripts.eval_model.build_dataset --limit 200 --seed 42

# 2. 跑推理（断点续跑，已有结果跳过）
python -m scripts.eval_model.run_eval --workers 4
python -m scripts.eval_model.run_eval --models workbuddy/glm-5.3-flash --limit 5 --dry-run

# 3. 打分（硬规则 + 双裁判 + 分歧裁决）
python -m scripts.eval_model.judge --workers 4

# 4. 出报告
python -m scripts.eval_model.report --baseline workbuddy/deepseek-v4.1-flash
```

## 产物（全部在 `temp/eval/`，已 gitignore）

| 文件 | 内容 |
|---|---|
| `dataset.jsonl` / `dataset_stats.json` | 抽样后的样本与过滤统计 |
| `raw/<model>/<sample_id>.json` | 单次生成结果（回复、耗时、usage、输入元信息） |
| `judged_all.jsonl` / `judged/<model>/*.json` | 每条样本的硬规则标记、双裁判评分、裁决结果 |
| `report.md` / `report.csv` | 总览、分场景、带图对比、配对检验、典型失败 |
| `review.csv` | 人工抽检队列（致命 / 分歧 / 低分优先） |

## 评分

- **硬规则**：编造 URL/域名/版本号（不在「金标准 ∪ 知识库」白名单内）、禁用词、兜底话术、空回复、超长、markdown 残留
- **LLM 裁判**：`workbuddy/kimi-k3.1-pro` + `workbuddy/mimo-v2.6-pro`，各出 0-3 分，取中位数；分差 ≥2 由 `workbuddy/step-5-preview` 裁决
- **指标**：严格正确率（3 分）、可用率（≥2 分）、0 分率、致命错误率、平均耗时、裁判失败率
- **配对检验**：与基准模型逐样本比较，McNemar 精确检验

## 已知限制

1. 金标准是人工回复，本身不保证最优 —— 结论是「与人工是否等价」，不是「绝对正确」。
2. 素材库清单未注入（需登录态），视频样本只保留文本占位。
3. 没复现订单信息注入与模型自主检索，事实类样本靠静态参考话术。
4. 参考话术已剔除与金标准相似度 ≥0.6 的条目（防答案泄漏）；检索无显著命中时宁可不注入。
5. 报告里的账号、密码、服务地址会打码；原始文本只留在 `temp/`。
