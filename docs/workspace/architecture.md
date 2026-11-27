# 工作区架构与维护接口

从下游使用者要获得的能力出发，沿各岗的决定和原件理解整个系统。维护原则见[正式入口](../../AGENTS.md)，当前工程与未决边界见[原维护计划](agent-autonomy-plan.md)。

## 从建设目标到数据库交付

| 环节 | 谁决定什么 | 基础设施承担什么 |
| --- | --- | --- |
| 选包与承诺 | 总主对照产业、库内覆盖和外部证据，选择这一版值得投入的技术、地区和用途。主审接下整包，判断怎样拆分和验收。 | 原 brief 与范围清单保留委托依据。明确改变用途、范围、终点、关键接口或授权投入时才修订冻结基线。普通回执归原账本。[四种边界](../../roles/COORDINATION.md) |
| 派工与接续 | 主审给作者一个能独立交付的结果。作者选资料、方法和工作顺序。等待某一依赖时，主审负责继续安排其他已有条件的工作。 | `work-request` 绑定任务、原件与收件人，`work-dispatch` 按实际宿主启动。`work-show` 从同一账本列当前责任、未办回件和等待条件。Hook 给原件地址。[业务入口](../../workflows/business-flow/README.md) |
| 制作与计算 | 作者判断代表性、参考量、功能单位、系统边界、数据源及合理代理，能交证据、工序、草稿或可复算模型，取决于原约定。 | Skills、天工 AI、LCA CLI、SDK、Foundry 和计算工具提供取证、构建、检查、差异与读回。程序能重算哪些字段、查到哪条引用就报告到哪里。资料、候选和推导留在原包。[数据方法](../../workflows/dataset-review/README.md) · [工具绑定](../tools/data-cli.md) |
| 独审、返修与采用 | 独立只读 reviewer 检查科学依据并将意见直接交作者，作者修订或解释分歧。原主审决定当前准确版本适合什么用途、哪些余项要继续投入。 | 固定送审正文、原意见和作者处理，提供差异与已知消费者。按约定登记三轮自主复审预算，第四轮投资归主审决定。按普通原文计轮。[独审](../../roles/reviewer.md) · [原件及差异](../../bin/scientific_review/delta.py) |
| 草稿、送审与反馈 | 作者在授权内保存并读回。主审采用成熟对象、组织获准的提交，平台作审核裁决。用户指定的轮询维护者汇总平台反馈，原主审接回包内问题。 | 核准确账号、对象、动作、关联范围、停止和实际效果。分别保留保存、读回、受理和发布的原生回执。结果未知沿原操作查证。[平台动作](../../workflows/dataset-review/送审准备与实际送审.md) |
| 运行与维护 | DSH、Devin 等宿主分别提供它们实际支持的 Session、Goal 或恢复能力。维护者修各岗共用的工具，原负责人判断业务能否接续。 | 宿主适配器、容量与写锁护住并发、写责及停止；原 Session 保留已有尝试。维护 board 记录共享故障和回验，安装记录界定本机资格。[宿主](../tools/production-host.md) · [维护职责](../../roles/maintainer.md) |

当前计划页给接手人方向，冻结稿是原承诺，`.control/harness.sqlite` 保存任务、决定和运行事件，原包和原生工具保有正文、推导及回执。进入任务先读其当前页和原委派，必要时查冻结稿或历史。完成一个作者请求、全员返回、平台受理，各自只说明相应事件。整包交付由原负责人对照使用目的和约定终点判断。[当前视图](../../bin/harness_snapshot.py) · [原件保管](../../NAVIGATION.md)

用户离线时，包内返修、采用、调整已授权投入和接续由原主审推进。跨包排序由总主按原授权处置，每两波核一次，有异常提前核。改变用户委托、追加总预算、扩写权限、恢复用户亲自叫停的生产或首次试用 Devin 做生产复审，才回到用户保留的决定。遇到其中一项，独立工作仍由其负责人继续。正式总主宿主冷接任和 CLI 关联范围的资格见[当前工程边界](agent-autonomy-plan.md)。

## 2026-11-22 对现役入口的核查

本节保存改造前的观察。现行行为与设计依据见[协作重构计划](../../reports/workspace/2026-11-22-harness-coordination-refactor.md)。四项隔离反例均用本仓库的原 CLI 或批次预演，每次使用临时任务库。它们界定需要调整的位置，本节不改变冻结承诺和运行权限。

| 入口 | 实际观察 | 应怎样调整，怎样验 |
| --- | --- | --- |
| 新科学派工 | 父合同已有排除边界，子请求只缺 `out_of_scope` 时，`work-check --for dispatch` 返回未就绪；仅补“无”即就绪。新启动实际读取这个结果。 | 同一范围内复用父合同和主审明确引用的委派，只表达子包差异。真正缺用途、终点或授权时指出影响；跨范围仍须有权人决定。用仅缺重复字段与实际越权两件分别验。[合同检查](../../bin/harness_scope.py) · [启动](../../bin/agent_runtime.py) |
| 正式采用 | 作者按 `work-return` 交普通正文，写明唯一范围 P0。主审在 `work-dispose --acceptance final` 指定 P0 并引用采用原件，仍返回 `coverage_required`。现象限最终采用。 | 在同一次采用中保存主审对原报告和原委派范围的明确关联，保留作者原说法及未覆盖项。核跨范围采用仍受限、要求平台效果的终点仍有准确回执。[采用入口](../../bin/harness_scope.py) |
| 科学检查展示 | 无索引的普通意见经主审具名采用后，`work-check --for handoff` 把科学独审、数量与单位含义、双语含义三行一起显示 `pass`，本例三行的 `source_refs` 均为空。实现把同一个 `science_ok` 用于两个专题。这会让读者高估逐项验证覆盖。 | 分别展示谁审了哪份正文、谁按什么用途采用、哪些专题确有原件支持。无单独专题依据时指向完整意见，保留实际检查范围。用只讨论某一问题的意见验另一项不会凭空通过，同时原主审仍可按已证用途采用。[交付检查](../../bin/harness_scope.py) |
| 批次中单独送审的 Flow | 主审在准确 Flow 的原采用理由写明独立比较用途，已具名批准该根送审。批次预演报 `usage_missing`；同一理由另登记为 `review-independent-use` 后变为 `ready`。两次均零发送。 | 让本次正常采用或送审决定直接引用同一负责人对该准确根的原用途判断。歧义只交主审核这一根，既无已知消费者又无独立用途则暂缓。技术授权和服务端关联范围仍分别核实。[批次入口](../../bin/platform_review/submission.py) |

另有一处调度风险。`guard_reserve` 会把任一尚未映射优先级的余项计入储备关闭条件，只影响首次启动已标为储备的请求。建议让主审复用冻结承诺里的核心范围和开启条件，判断该余项是否影响本次储备。真实核心仍可推进、储备超出预算或开启条件不明时，保留原限制。这里有源码依据，尚无此条件造成生产延误的证据。[储备准入](../../bin/harness_wave.py)

此前升级中，冻结与当前页分离、已读回件仍待办、普通独审原文计轮及写后未知效果沿原操作恢复，均有对应反例和离线回验，值得保留。W2 加隔离用例证明工程行为。整波无人催办、实际交付周期与科学返修质量，留到获准复产后一起观察（详见[波次执行设计评审](../../reports/workspace/2026-11-22-wave-execution-design-review.md)）。

## 日常接口

```sh
.venv/bin/python bin/harness.py work-maintenance
.venv/bin/python bin/harness.py work-packages
.venv/bin/python bin/context.py --task TASK_ID
.venv/bin/python bin/run_worker.py --request REQUEST_ID --read-result
```

[业务入口](../../workflows/business-flow/README.md)记录明确的委派、采用和接续；[生产宿主](../tools/production-host.md)执行原生调用。索引和结构摘要供定位，原报告和未能解释的原文始终可读。技术运行成功、Goal结束、科学交付及平台完成分别依据各自事实。

维护公开入口时可从实际调用追到其实现。任务读写由 `harness.py` 经 `harness_tasks.py` 进入 [task_ledger](../../bin/task_ledger/store.py)。候选检查 `lca_gate.py` 使用 [dataset_checks](../../bin/dataset_checks/projection.py) 核对交换和引用。独审使用 [scientific_review](../../bin/scientific_review/facts.py) 读取同一候选、原意见和本轮差异。宿主调用、Hook 和 Skill 使用各自已登记的入口。

## 原件和工具

在同一工作目录直接读原件。已有包原址继续；必要新材料按[材料存放说明](materials.md)选择位置，在原任务链接。共享消费者复用同一来源。存储登记、生命周期表单与兼容迁址程序已退出执行，历史记录保持原址和Git可查。

[本地工具](../tools/data-cli.md)选择当前安装；只初始化实际调用的工具。安装和解析缺口限制对应能力，已有成果和任务不依赖旧安装。保留具名写入授权、原数据操作及恢复状态，工具升级不重放写入。天工系列上游缺陷经英文 issue 和正式更新解决；其他工具的本地接入由维护者负责，对外协作按用户另行委派执行。

旧 handoff、管理阶段、上下文装配和控制协议不再作为执行入口。实际搬迁或换机使用所属工具支持的恢复方式，保全原 Session／attempt、停止与未知效果。版本、备份与LFS材料见[版本管理](versioning.md)。

## 从命令定位实现

| 工作 | 实际入口 | 实现及仍直接使用它的调用方 |
| --- | --- | --- |
| 委派、范围、采用、接续 | `harness.py`、`run_worker.py`、`context.py` | [任务账本](../../bin/task_ledger/)集中保管存储、业务关联、决定、原会话、维护单和接任。顶层 `harness_tasks`、`harness_scope`、`harness_wave`、`harness_snapshot`、`harness_package` 也供原包与独立资格程序读取。 |
| 资料发现、维护观察、宿主 Hook | `harness_knowledge.py`、`harness_feedback.py`、`harness_hook.py`、`harness_context_hook.py`、`harness-hook.ps1`、`devin_context_hook.py` | 资产、Skill 和旧方法查询在 [workspace_queries](../../bin/workspace_queries/)。`workspace_files`供查询、运行器和独立检查解析工作区路径。 |
| 科学独审与平台审核 | `review_job.py`、`review_adapter.py`、`review_capture.py`、`harness.py work-submit-batch` | [scientific_review](../../bin/scientific_review/)负责请求、意见、差异、预算和投递。[platform_review](../../bin/platform_review/)负责平台审核读取和获准根的送审。`production_review`也是既有工程验证调用的科学复审接口；`delivery_evidence`用于读取保存、提交与读回事实。 |
| 运行器、身份和容量 | `agent_runtime.py`、`collaboration_mcp.py`、`native_cli_capture.py`、`dsh_recovery_host.py`、`dsh_capacity_handoff.py` | `harness_hosts`按本机注册选择 [Claude 适配器](../../bin/native_hosts/claude_adapter.py)、`codex_adapter`、`devin_adapter` 和 `dsh_headless`。`agent_base`、`codex_host`、`devin_acp`、`collaboration_tools`承接相应宿主协议；`dsh_capacity`、`dsh_auto_resume`、`execution_lease`负责并发席位、恢复与单写锁。 |
| DSH 原生插件与观察 | `dsh_native.mjs`、`dsh_packages.mjs` | 对话上下文、调用、写入权限和工具交付插件在 [dsh](../../bin/dsh/)。`dsh_goal.mjs`、`dsh_sse_diagnostics.mjs`供原生 Goal、停流资格驱动直接导入；`safe_diagnostics.mjs`与 Python 的`safe_diagnostics`分别用于相应宿主的诊断输出。 |
| 数据 CLI 与本机工程 | `launch.py`、`env_run.py`、`lca_gate.py`、`repository.py` | [dataset_checks](../../bin/dataset_checks/)负责数据投影、引用及写入授权。`tiangong_cli_tool`、`local_check`、`utilities_tools`由宿主工具与安装验证直接导入。 |

维护者的[全库链接检查](../../roles/maintainer/scripts/check_links.py)随岗位保管。用项目 Python 安装同目录的 requirements.txt，再运行脚本并以 --report 指定输出。它发现所有 Git 可见的 Markdown，分别报告工作副本或指定提交的全部缺失目标，以及现行指南中的数量。此检查用于维护，不参与业务派工。
