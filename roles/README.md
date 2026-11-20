# 职责入口

所有 agent 在原工作区直接读取原任务和本职文件。职责由用户任命或原委派决定，程序不从长句、目录名称或当前状态推断职责，也不生成另一份职责文档。

| 职责 | 直接读取 |
|---|---|
| 总主 | [总主](global-main.md) |
| 任务主审 | [任务主审](task-lead.md) |
| 完整工作包执行 | [执行职责](data.md) |
| 科学独立审阅 | [审阅职责](reviewer.md) |
| 维护负责人 | [维护职责](maintainer.md) |
| 用户指定的轮询维护者 | [平台送审后的意见汇总](platform-feedback.md) |

执行者按本次工作直接取所需活动原文：[科学制作](scientific-production.md)、[数据支持](data-support.md)、[计算](calculator.md)、[工程执行](engineering.md)。从 [LOCAL_TOOLS](../LOCAL_TOOLS.md) 选择工具并读取适用 Skill；科学判断读[数据规则](DATA-RULES.md)，实际交接读[协作边界](COORDINATION.md)。

原 brief、成果和证据沿原路径读取，运行器只传递准确文件位置。当前状态通过原 harness 查询；已有任务或请求可用 `context.py --task ID` 或 `--request ID` 取得结构化记录。
