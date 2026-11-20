# 方法与经验原件

Agent 按当前问题选择并读取完整方法、案例和证据，判断其适用性。维护者根据实际反馈修订共享方法。查询提供文件位置、原登记和实际使用记录，不抽取正文、推导推荐资格或给任务注入必读上下文。

`python3 bin/context.py --task TASK_ID --query "问题"` 查询原任务及工具、资产入口。`--scope ID` 读取明确的原路线，`--asset ID --asset-version VERSION` 查原登记版本，`--method guidance:quantity-basis` 返回当前方法文件位置。已有准确路径时直接读原件。原历史目录缺失只影响依赖它的查证。

有复用价值的观察直接写在原返回或原方法文件中，说明问题、证据与用途限制；不要求 method_uses、remaining_branches 或生命周期表单。`harness_knowledge.py discover --task TASK_ID` 可查已有原文关系索引，记录按原样保留，索引不会成为准入或采用门槛。

## 原案例

- 宁国铸造的两种年度量来自不同的设计口径。按原源表与数量链判断各自适用范围，复用已有的单位化影响核对。[两个年量的来源](</home/fpddmw/projects/htam/materials/packages/lca-todo-discovery-20261030/v2-native-validation-20261108/README.md#两个年量的来源>)
- HyInHeat 的工段代理投入、覆盖边界和说明与正文的差异有完整原件。按实际工段核对来源与入账，保留原审结论及条件。 同一回放保留文件 SHA-256 和载荷 SHA-256 的实际比对。[原件只读回放](../../materials/packages/lca-todo-discovery-20261030/v2-native-validation-20261108/hyinheat-original-readback.json)

- 广美 CO₂ 修订中，交换值更新后原量账仍显示早先数值。沿原复盘核实际影响和恢复点。[苹果执行复盘第 2 节](</home/fpddmw/projects/htam/workspaces/lca/apple-recycled-materials/runs/2026-10-30-todo-handoff/execution-efficiency-review-20261106.md#2-目标已有基础与实际增量>)
