# data-workspace任务入口收敛：协调与P0维护入口

本页面向协调者与维护者，用于整理作者任务入口、选择skill/CLI/Foundry路径及核配套资格。**有界数据作者从原科学包的当前任务入口开始，不把本维护页作为默认执行任务。** 科学执行复用[现有工作流](../../roles/WORKFLOW.md)，工程交[Issue工作流](../../workflows/workspace/engineering-repair/README.md)。

## 当前入口

[原设计与计划](../../materials/packages/deepseek-data-workflow-design-20261115/DESIGN.md)保管这次有限目标；
[P0核查结果](../../materials/packages/deepseek-data-workflow-design-20261115/p0-baseline-20261115/README.md)保管实际命令、来源、Issue交接和验收边界。
当前本地方案将任务入口和阶段上下文收敛设为主线，权限隔离保留为配套；见[现行作者入口](../../roles/data.md)和[重写后的本地Issue稿](../../materials/packages/deepseek-data-workflow-design-20261115/p0-baseline-20261115/issues/harness-blocked-issue.md)。现有角色/阶段入口已完成[本地文档落实与静态核验](../../materials/packages/deepseek-data-workflow-design-20261115/entry-convergence-20261115/README.md)，未改远端Issue、工具接口或运行配置。锌试点仍未启动，线上数据不改。

## 协调者与作者分别从哪里进入

- 总目标、跨包选择及仓库维护：运行 `python3 bin/context.py --role coordinator`，按返回的协调者合同选择科学包/阶段，再在原brief或工作项中提供作者视图。
- 已指派的有界数据包：运行 `python3 bin/context.py --role data`，读取返回的data合同及原brief/当前工作项，按[阶段阅读图](../../workflows/dataset-review/README.md#按当前阶段读取)展开。已知原件优先，`--scope`/routes只负责定位，不负责给作者选择新任务。
- 文档和导航已接通不代表作者行为已改善；C15—C18比较及锌试点仍未运行。

## 协调者使用顺序

1. 协调者先定位原目标、原owner、当前工作体和本轮科学包/阶段；用已有context发现准确原件，不把导航结果直接当作已选执行任务。
2. 协调者据[规则基线](../../materials/packages/deepseek-data-workflow-design-20261115/p0-baseline-20261115/rules/README.md)及[P0实际入口证据](../../materials/packages/deepseek-data-workflow-design-20261115/p0-baseline-20261115/runtime/README.md)选择适用skill分支/工具路径，处理管理冲突和未支持项。
3. 在原包brief/阅读入口给出作者启动视图。作者采用已有data职责，执行当前阶段；必要来源和依赖完整可得，跨包/工程问题回协调者，正常科学探索不逐工具审批。
4. 阶段完成或输入变化时，从原owner核事实并更新相关视图，复用有效证据；原生工作项/Task/attempt保持唯一归属。
5. 配套工具和无写权环境依[实施计划](../../materials/packages/deepseek-data-workflow-design-20261115/P0-P1-IMPLEMENTATION.md)另核实际采用。试点仍须有明确启动指令，不因文档或Issue存在自动运行。

现有research环境过滤、独立会话、account_intent=null均不能单独证明凭据/网络隔离。生效门禁与审查材料应由作者不可读改的真实权限保护；未验证前如实标为待资格，不写“已就绪”。

本页是发现入口，不是另一套scheduler、状态机、validator或授权表。实际工具和Task事实由原owner保管。
