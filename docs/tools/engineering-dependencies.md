# 工程依赖：定位原请求与证据

这是数据工作遇到工具缺陷时的历史证据定位入口。当前执行先读[工程修复工作流](../../workflows/workspace/engineering-repair/README.md)，其中有新会话／候选返回模板、现有 harness 命令和 PR 证据模板。操作范围由当前 [engineering 角色合同](../../roles/engineering.md) 与相应政策原文决定；本页不维护第二份工程状态表。

先在工作区根目录展开当前规则和授权原文；已有准确请求时直接沿原请求，只有实际持久交接需要时才读取现有工程登记：

```sh
python3 bin/context.py --role coordinator --policy-key engineering_delivery.mode
python3 bin/context.py --role coordinator --policy-key engineering_delivery.authorization
python3 bin/context.py --role coordinator --policy-key engineering_delivery.delivery_conditions
# Only for an actual engineering handoff:
python3 bin/harness.py engineering-list
```

从返回的原 engineering ID、assignment、result 与验证路径选定具体问题。已有数据问题先按数据合同处理；确属工具缺陷时，当前流程为先 Issue、新独立工程会话、最新上游修复、原数据流程候选验证、本地预审、再次核验基线后提交 PR，并跟进线上 review 返修。具体规则和证据要求只维护在工程角色合同，不从旧 Issue 或本页推导。

原生复测和运行时采用证据属于原记录。Issue 的历史关闭、PR 测试或发布包存在，均不能单独证明当前数据阻断已解除。未知写入结果仍由原 task/attempt 恢复。

## 按问题取用历史证据

以下两项是定位示例，日期属于原证据；2026-10-05 整理时没有重新查询上游或运行数据任务。

| 问题 | 原观察/回执日期 | 精确证据 |
|---|---|---|
| Model 前序版本与正式发布谱系 | 2026-09-24；Issue 评论回执为 06:33:27 UTC | [CLI #348 评论](https://github.com/tiangong-lca/cli/issues/348#issuecomment-5809014546)、[原始回执](../../work/engineering/ENG-LCM-PREDECESSOR-20260921/issue-lineage-policy-followup-20260924.receipt.json)、[当时的职责调查](../../work/engineering/ENG-LCM-PREDECESSOR-20260921/draft-rule-owner-review-20260924.md) |
| Process 审核单对象/数组的工具支持 | 2026-09-25；源码核验为 07:46:44 UTC | [上游核验](../../work/engineering/ENG-PROCESS-REVIEW-CARDINALITY-20260925/upstream-inspection.json)、[原 Issue/PR 记录](../../work/engineering/ENG-PROCESS-REVIEW-CARDINALITY-20260925/upstream-owner-records.json)、[CLI #350](https://github.com/tiangong-lca/cli/issues/350) |

其他既有条目的地址保留在[旧工程索引](../../reports/tooling/2026-09-26-engineering-index.md)。其中“当前”“下一步”和维护说明属于旧观察语境；本次未重判这些任务状态，也不据此恢复业务。新材料按 [NAVIGATION](../../NAVIGATION.md) 放入所属项目的短运行目录，不继续扩张旧类别树。

本页是正式维护入口；旧索引及各 ENG 目录中的 assignment、result、Issue 回读、harness 和原生验证证据保留。目录整理不改变原请求身份、单写者、权限或候选科学结论。
