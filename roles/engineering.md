# 工程职责

先用 `context.py --request ID` 打开原 finding 或准确工程委派。按[执行工作流](../workflows/workspace/engineering-repair/README.md)查当前步骤和返回点。

## 维护负责人

职责、投入和终点见[维护指南](maintainer.md)。按实际操作取同一原入口。

工作区修复默认由维护者亲自完成，包括共享运行时、恢复语义及数据操作边界的缺陷。按受影响行为做复现、回归和原工作链验证，不把另找非作者或新开工程会话设为通用前置条件。工程委派和跨会话协作按[维护指南](maintainer.md)及当前用户指示；上游仓库交付沿适用的原 Issue／PR 流程。已绑定的委派保留原责任和验收。

- `work-maintenance` 取本次队列和精确观察，`harness_hook.py link --finding ID` 归并已确认同因的观察，`--dismiss` 保存普通调用问题的纠正依据。
- 在原 finding 下直接修复并返回证据；不为本人实施新建工程子任务。确已获准且需要委派时，`work-maintenance-delegate` 绑定原 finding、真实执行会话、范围及验收。模型与推理档位沿本次明确选择，不由角色名决定；已有 Issue、PR 和执行会话按准确归属接续。
- 按[原入口回验](../workflows/workspace/engineering-repair/README.md)核实际运行版本、失败步骤及后续可用性；需要真实业务操作的回验由原拥有者在原授权内继续。原 finding 保存修复和实际回验依据。科研暂停期间可继续隔离工程修复，业务停止、在途和未知按[现有状态](../workflows/business-flow/README.md#历史记录)保留。

## 工程执行

1. **核准确委派。** 从本人真实会话读原 brief、所属仓、当前基线、原反例、验收及返回点。用明确绑定的工程范围和原工作目录内的本地分支直接实施。缺关键原件时交维护负责人补清，继续独立可行部分。
2. **复现并修复。** 优先重用已有上游修复。沿真实公共入口找出最早的失败边界，修订负责模块并跑受影响回归及后续动作。例如依赖同版但构建哈希变了，要核原 wrapper 选择和真实输入。将候选身份、选择方式、原生输出及剩余限制交原维护负责人集成和回验；工程审阅沿本次明确约定。
3. **按已授权的终点交付。** 本地候选沿原 `work-return` 或原生终态返回，给准确 diff、运行证据及恢复点。授权覆盖 PR 时，按[工程交付](../workflows/workspace/engineering-repair/README.md)整合新鲜 canonical SHA、复验、推送和读回同一 PR；实质线上反馈回当前候选和必要复验。
4. **交付或说明受阻部分。** 完成授权内可做的实现、验证及返修后沿原请求交回；外部条件只影响对应步骤，明确原件和接续条件。原调用方决定业务采用。已有 PR 或工程交付的实质返修沿同一责任；已交付结果的替代保留原代次和证据。

## 授权与保留边界

原用户任命、原 finding 指派及当前决定共同确定工程范围。具名工程授权可覆盖对应 Issue、工程委派、源码、本地候选、功能分支及 PR 反馈；准确指派或本轮具体限制作准。合并、正式发布／部署及全局默认运行替换各核适用授权。数据权限沿[工作区边界](../AGENTS.md#权限和恢复)，科研原身份、运行、证据、凭据和安装按原拥有者保全。

按需展开 `context.py --policy-key engineering_delivery.authorization` 及 `engineering_delivery.delivery_conditions`。按前者返回的路径与 SHA-256 取原授权。工作树未包含该原件时，可用 `git show e68e5cb8c:materials/packages/engineering-delivery-policy-20261009/authorization.json` 取相同字节，核对后使用。原工程路由由 `.control/engineering.json` 定位。
