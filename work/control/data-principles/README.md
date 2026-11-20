# 数据开发默认原则

范围：跨案例的用户取舍、长期默认值及其实施位置。维护者：Coordinator。上级：[范围与协作](../README.md)。

当前为 `TG-DATA-DEFAULTS / 3`。2026-10-30用户要求保留 Platform 年度量保存形式，并将依据、推导和限制放入其他稳定字段；年量必填、真实来源优先及固定假设／reference flow 回退的科学要求继续有效。

| 入口 | 内容 |
|---|---|
| [当前正式原则](PRINCIPLES-v3.md) | P2采用Platform格式与方法证据分工；P1、P3—P5保持不变 |
| [本版用户指令与授权](authorization-v3-20261030.json) | 保留Platform形式、调整数据研制字段映射；不扩大数据权限或授权改Platform |
| [本版接入与验证](adoption-v3-20261030/verification.json) | 当前原则选择、生成流程和精确变更验证；原生工具兼容状态单独说明 |
| [字段规则的分支兼容验证](../../../materials/packages/platform-field-compatibility-20261030/README.md) | 同一 v3 正文和授权原件；候选的模板、流程和验证范围单列 |
| [字段映射与往返验收](../../../roles/CANDIDATE-PREPARATION.md#process-annual-quantity-and-platform-round-trip) | 年度数值含义、稳定方法字段、旧说明无损迁移及CLI格式冲突 |
| 历史原件 | [版本2](PRINCIPLES-v2.md)、[其授权](authorization-v2-20260926.json)与[接入记录](adoption-verification-v2-20260926.json)；[版本1](PRINCIPLES.md)、[原五项批准](authorization.json)及[接入记录](adoption-verification.json)均保留 |
| [原讨论稿](proposal.md) | 审议时的阻断筛选与建议，保留原字节；其中“尚未生效”描述的是批准之前，当前以正式原则为准 |

`.control/policy.json` 绑定正式原则和批准证据。新 Agent 启动入口只核验并返回当前版本及哈希；在科学／数据工作前使用 `context.py --role <role> --principles` 读取完整原文，版本变化时重新加载。旧接入回执保留其当时的行为记录。原则原件与授权字节不变；当前启动合同以 AGENTS.md 为准。

Skill执行方法保持原安装版本；Foundry按当前公开brief/interaction和semantic采用回执记录具体任务所用原则。安装技能、bootstrap锁、历史协议及执行回执未改写，也没有新增全局Foundry开关或修改任务状态。已有任务继续遵守各自具体限制和原尝试；原则不自动启动旧任务。
