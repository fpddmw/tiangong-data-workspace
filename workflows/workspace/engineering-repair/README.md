# 工程修复

维护者从 `work-maintenance` 和原 finding 读取实际失败、影响、在途状态及恢复点，按[维护职责](../../../roles/maintainer.md)亲自施工。原任务作者修本次输入和调用；公共缺陷修负责模块，检查同因入口。

在同一工作目录用本地分支实现。先复现原反例，再验证实际公共入口和原先受阻的后续动作。未知操作沿原 attempt 查询，已成功写入不重放。需要生产数据动作的回验由原拥有者在原授权内完成。

修复记录沿[原 board](../../business-flow/README.md#维护发现的正常接续)保存，说明修改、适用测试、实际加载版本、原入口证据及尚未核实部分。`work-maintenance-note --request FINDING_ID --note 修复记录.md --status fixed` 直接记原文和本次决定；真实回验后再记录 verified。结案由负责 agent 根据证据判断，程序不通过报告字段推导资格。源代码、候选、合并、安装和业务采用分别记录。

## 本地预审和 PR 门禁怎样衔接

当前负责者检查最终差异、适用测试与原反例。用户授权包含上游 PR 时，提交准确变更和验证说明，核 CI 及原 PR 意见并继续修订。另开审阅或工程 agent 仍需本次明确授权，不作为本地修复的通用前置条件。已有 PR 与责任沿原件接续。

旧工程 claim/assignment/result 状态机不再用于新执行；原记录通过 `harness.py history --table engineering_routes --id ID` 只读查证。当前任务与 native Session 继续保有实际运行及停止事实。
