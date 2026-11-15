# ADR-0010：经 OpenAI 兼容端点接入现有模型网关

- 状态：已采纳（2026-11-04 实测后确定，2026-11-14 由原决策记录合并）
- 相关：[ADR-0005](0005-langchain-single-agent-loop.md)

## 背景

项目只使用现有的本地模型网关和额度，不新增付费接入。调查需要工具调用和结构化终态。

## 决定

1. 使用网关的 OpenAI 兼容端点 `/v1/chat/completions`。2026-11-04 探测显示，该端点在提示词与 schema 冲突时仍返回合法 JSON；Anthropic 形状的 `/v1/messages` 会接受 `response_format`，但不强制执行。
2. 模型 ID 通过 `VIGIL_MODEL_NAME` 配置，运行记录保存网关实际返回的模型。
3. 结构化输出由 schema 约束，客户端再校验并做有限纠错。
4. 额度不足作为阻塞登记，不改用付费服务。

## 后果

- 一次探测不构成长期保证；网关下线或替换模型时，需要重新验证工具调用和结构化终态（`scripts/skill_runtime_gate.py`）。
