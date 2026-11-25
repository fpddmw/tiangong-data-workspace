# 上游工具维护与回验

共享维护者用已有故障检查天工 LCA CLI、SDK、Skills、平台和天工 AI 的版本。查清原输入、失败版本、现行正式版本及实际部署，再用原入口和正常对照复验。工具更新后，旧成果和科学意见仍可从原文判断。写入结果未知时查同一次操作及其回执。

确认是天工上游能力或数据契约缺陷时，先查原 issue，同因补原单。用英文给出脱敏复现、实际与预期结果、根因证据、可行修法和验收要求，可提及已确认的负责人。维护者跟踪正式修复，把发布版本接到工作区的实际调用、Skill 与安装记录，再请原包核验。默认采用官方版本，上游的 PR、部署及非天工项目的对外协作依用户另外明确的委派。DSH、Codex 等宿主接入问题先修本地接口。

有明确工程代码委派时读[工程执行职责](../../roles/engineering.md)及[工程交付方法](../../workflows/workspace/engineering-repair/README.md)。将本地版本、复现、回验和仍受限的动作回到[原维护单](../../roles/maintainer.md)。

## 按问题取用历史证据

以下两项是定位示例，日期属于原证据；2026-10-05 整理时没有重新查询上游或运行数据任务。

| 问题 | 原观察/回执日期 | 精确证据 |
|---|---|---|
| Model 前序版本与正式发布谱系 | 2026-09-24；Issue 评论回执为 06:33:27 UTC | [CLI #348 评论](https://github.com/tiangong-lca/cli/issues/348#issuecomment-5809014546)、[原始回执](../../work/engineering/ENG-LCM-PREDECESSOR-20260921/issue-lineage-policy-followup-20260924.receipt.json)、[当时的职责调查](../../work/engineering/ENG-LCM-PREDECESSOR-20260921/draft-rule-owner-review-20260924.md) |
| Process 审核单对象/数组的工具支持 | 2026-09-25；源码核验为 07:46:44 UTC | [上游核验](../../work/engineering/ENG-PROCESS-REVIEW-CARDINALITY-20260925/upstream-inspection.json)、[原 Issue/PR 记录](../../work/engineering/ENG-PROCESS-REVIEW-CARDINALITY-20260925/upstream-owner-records.json)、[CLI #350](https://github.com/tiangong-lca/cli/issues/350) |

其他既有条目的地址保留在[旧工程索引](../../reports/tooling/2026-09-26-engineering-index.md)。其中“当前”“下一步”和维护说明属于旧观察语境；本次未重判这些任务状态，也不据此恢复业务。新材料按 [NAVIGATION](../workspace/materials.md) 放入所属项目的短运行目录，不继续扩张旧类别树。

本页是正式维护入口；旧索引及各 ENG 目录中的 assignment、result、Issue 回读、harness 和原生验证证据保留。目录整理不改变原请求身份、单写者、权限或候选科学结论。

## 原工程授权

按需展开 `context.py --policy-key engineering_delivery.authorization` 及 `engineering_delivery.delivery_conditions`。按前者返回的路径与 SHA-256 取原授权。工作树未包含该原件时，可用 `git show e68e5cb8c:materials/packages/engineering-delivery-policy-20261009/authorization.json` 取相同字节，核对后使用。原工程路由由 `.control/engineering.json` 定位。
