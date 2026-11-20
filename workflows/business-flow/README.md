# 完整任务的日常入口

总主持续安排父目标、投入和跨包依赖；主审组织整包交付、用途采用及获准平台提交；作者在原Session/Goal内自主制作、自查、科学独审和返修；独审核准确候选与用途，原文直接交作者；维护者亲自修公共能力。所有人在同一正式工作区读原件。程序保管身份、哈希、关联和实际运行事实，并在相应动作边界集中检查。

## 从原范围接续

[总主](../../roles/global-main.md#委派与持续交付)从原建设计划判断本版覆盖和下一笔投入，队列清空时也从这里接续。[主审](../../roles/task-lead.md)从原委派、完整 brief、合同和整包当前计划接回这一批成果。`work-show --id TASK`、`work-scope --task TASK`、`work-packages --task TASK` 及 `context.py --task TASK` 共用已登记范围的完整视图，可核待确认的关联、未派、已承担未交付和有返回待采用的责任。

五态分别是未安排、已承担待交付、已验收、本方已提交、原委托方已处置。关系未核显示unknown，不预填完成。承接、作者明确返回覆盖、主审采用和最新已观察执行分别看；“已承担”不表示仍运行，局部返回不外推全request。历史采用未注明stage/final时保留原意义，显示验收层级待核。

Hook按启动、恢复、压缩及重要事实变化提供短职责卡、完整计数和准确入口；卡片和列表是预览。结论与对象、版本、用途及限制一起读取，未展开不代表没有限制。使用`--detail`取原事件及准确原件；`work-show/work-packages/work-scope/context --output /ABS/FILE.json`完整落盘，避免长stdout丢失。

## 登记范围与一份合同

[主审](../../roles/task-lead.md)在自己的会话中用原生子智能体协助定制作计划。确定要制作的对象、用途及验收边界后，再用下面的 `work-request` 和 `work-dispatch` 派发制作包。

```sh
.venv/bin/python bin/harness.py work-create --brief 原目标.md --owner 主审THREAD_ID --contract 合同.json
.venv/bin/python bin/harness.py work-scope --task TASK --apply 范围与关联.json --dry-run
.venv/bin/python bin/harness.py work-scope --task TASK --apply 范围与关联.json
.venv/bin/python bin/harness.py work-request --task TASK --brief 完整子包.md --contract 子包合同.json --scope SCOPE_ID
.venv/bin/python bin/harness.py work-check --request REQUEST --for dispatch
.venv/bin/python bin/harness.py work-dispatch --request REQUEST --execute
```

合同集中写`purpose`、`endpoint`、`scope_boundary.in_scope/out_of_scope`、`acceptance.text/refs`、`authority_refs`及可空的`interfaces.text/refs`。无额外排除也如实说明；不匹配固定用语。完整brief承载产品/参考基准、证据、已有资产和背景，作者选择方法、工具与迭代。引用可给原文件路径，由工具保存哈希；授权引用不是新增权限。

本次请求可用 `work-dispatch --config 配置路径 --execute` 明确选择宿主；[Devin/SWE-2配置与接续](../../docs/tools/devin-host.md)复用同一任务、合同、范围、原成果和返回。Devin与DSH各保有真实Session，跨宿主接手用同任务的新明确委派，先查原在途/停止/未知尝试；不把新宿主伪装成原Session恢复。全局DSH绑定不因单次选择改变。

终点为`evidence | candidate | saved_draft | submitted | published`：分别是可验收证据、本地候选、合格草稿保存及完整读回、本方正式提交、实际发布。父任务与子包可有不同终点；作者到达子终点后，主审继续父目标。研究或本地任务不强制保存；提交不等于专家通过或发布。

派包按[四种边界及原字段](../../roles/COORDINATION.md#用途与四种边界)表达使用者、用途增益、本版承诺、本包负责、物理/计量接口、已含背景、已有适配和待承接责任。共享供方沿原件建设一次，各消费者用原scope/source_refs及interfaces保留需求、准确版本和本方采用；跨Task不能把别人的request挂成本任务承接。全部子包final也不能自动缩减父scope。

**工程例子。** 父包承诺干基粉体供给，原scope分别有“煅烧模块证据”和“成品粉磨/供给”；给作者的子合同可写`purpose`为窑门煅烧模块、`endpoint:evidence`，interfaces交原料含水、每吨窑门产品、已含燃料及外部粉磨需求。作者只覆盖前项的返回可由主审最终采用；后项尚未承接时父包仍未完成。若父包已承诺干燥的燃烧排放，子合同改称“有限能耗”不会自动移除该责任。以上是接口/责任示例，不是新增生产委派、科学参数或统一必填项。

范围apply包含`items:[{scope_id,kind,label,source_refs,target_endpoint?}]`、`links:[{request_id,scope_ids}]`、`verified_scope_ids:[]`和`source_ref`。kind仅requirement/deliverable。提交为增量upsert，不按缺席项删除已登记范围。只有逐项核清原承接与覆盖关系后才登记verified；范围缩减、取消或替换仍需原委托方明确决定。历史未结构化项保留出处，不能猜成未派或完成。

`work-contract --task TASK`或`--request REQUEST`查看；原委派者/有权负责人用`--set 合同.json --source 原依据`修订。草案可先保存缺项，首次新科学派发集中指出缺口。历史原Session读件与技术接续沿原brief和有效决定，不以补新格式为前提；真正扩展范围先更新合同。`work-enroll --role ROLE --authorization 原任命`只登记本次真实任命，不转移原主审、写责或凭据。

## 派工后的等待

主审把具备开工条件的一批工作集中派出。活跃时处理能推进的待办，及时接回提前返回的成果，按需要采用、安排返修或续派。

**手头无事可推进、只剩等待在途工作或外部依赖时，立即一次休眠满 30 分钟。** 休眠从进入纯等待时开始。把整段等待交给宿主，短时心跳和等待句柄的 `pending` 留在宿主内处理，模型到点再恢复。醒来集中查看一次原运行与新回执。有事就继续做，仍只剩等待就再休眠满 30 分钟。

## 作者与独审

作者按合同核库取证、制作、自查，并用`request_review`给出准确候选路径与科学问题。有原生Goal的宿主自行维护Goal；Devin按原brief和合同在持久Session的一次调用内连续推进，当前没有已验证的Goal API。宿主冻结候选、保留实际独审身份，启动独立只读OS作业；作者调用结束不取消独审。同模型家族允许，不能复用作者Session自审。

同一委派和对象的返修可通过 `prior_review_id` 指定已返回的原独审。独审可直接读取原意见、两次冻结稿、差异和已知消费者。另用 `supersedes_review_id` 指定同一作者的一个未开始旧审。新版登记并冻结后，原队列核准撤换。已领取、已完成或归属待核的旧审返回其实际状态；作者可继续原工序或读取原审。

`review_result`默认立即观察，显式`wait_seconds`可选0–600秒并可取消。取件先核原生/已落盘事实，保留原review_id；安静不等于死亡。作者继续独立工作，意见到达后返修、复核或有据解释分歧。部分意见可读，失败或中断不冒充完整审阅。

其他宿主或缺少便捷工具的旧线程使用同一公开入口，不换作者、不重做成果：

```sh
.venv/bin/python bin/harness.py work-request --task TASK --review-of AUTHOR_REQUEST --brief 科学问题与冻结候选.md --contract 审阅合同.json
.venv/bin/python bin/harness.py work-dispatch --request REVIEW_REQUEST --execute
.venv/bin/python bin/run_worker.py --request REVIEW_REQUEST --read-result
.venv/bin/python bin/harness.py work-review-record --return-id REVIEW_RETURN --conclusion supported_with_limits --basis 原意见.md
```

该审阅brief给出冻结候选及准确科学问题；合同沿上面的同一字段声明只读审阅范围和evidence终点。旧线程读件不要求补格式，新开审阅的首次派发仍使用同一集中检查。

科学结论有supported、supported_with_limits、material_issue、not_assessed四类，说明准确用途、候选、依据、问题或未评估含义。明确原意见可由接收者一次索引，自动保留原审阅者与冻结候选，不需唤醒审阅者补标签；原文未作判断时保持未评估。受限结论是否符合本任务用途，由主审明确采用。

完整复审原文明确解决并替代哪份原意见时，有原任务索引权限的负责人可在同一`work-review-record`加`--supersedes 原REVIEW_RETURN`（可重复），依据仍用该完整新意见；工具核同一作者责任链、先前独审及全部原对象身份被新冻结包覆盖。两份原文、结论及辅助报告均保留；纯文字原委派复用其已冻结索引，规范数据对象才定义此处的目标身份。取代只对新意见实际覆盖的当前正文生效。没有明确取代关系时不按时间新旧清掉material_issue；也不让已明确解决的问题永久阻断未变成员。索引不替代主审用途采用。

## 随时返回、采用与完成

```sh
.venv/bin/python bin/harness.py work-return --request REQUEST --result 原报告.md --scope SCOPE_ID
.venv/bin/python bin/harness.py work-return --run RUN
.venv/bin/python bin/harness.py work-return --run RUN --candidate /ABS/候选.json --artifact /ABS/原生回执.json
.venv/bin/python bin/harness.py work-dispose --return-id RETURN --use adopt --decision 用途与限制.md --scope SCOPE_ID
.venv/bin/python bin/harness.py work-dispose --return-id RETURN --use adopt --decision 原用途采用.md --scope SCOPE_ID --apply-review 原REVIEW_RETURN --handling 原作者处理.md
.venv/bin/python bin/harness.py work-check --request REQUEST --for handoff --summary
.venv/bin/python bin/harness.py work-dispose --return-id RETURN --use adopt --acceptance final --scope SCOPE_ID --decision 最终采用.md
.venv/bin/python bin/harness.py work-complete --task TASK --note 完成依据.md
```

阶段成果、局部报告、独审意见和失败任何时候均可接回；scope可选，缺覆盖元数据不拒收，不据final措辞推出全量覆盖。adopt/revise/hold/reject保存原用途决定；adopt默认stage，不完成范围。final只用于准确版本、明确覆盖与终点检查满足的采用。`--checks`可引用本服务生成且指纹仍匹配的检查文件，手填ready/pass不生效。原生工具仍核真实账号、所有权、前像、精确授权及写后读回。

有多个历史版本时，重复 `--candidate` 选定本次交付的完整准确候选集合。可提供规范正文或带完整正文的准确原生读回；`--artifact` 单独关联保存、独审和审核记录。已有旧版本及意见留在原记录，同一对象的后继版显示原意见和改动，新版的科学采用依原作者处理和主审有据判断。父范围另需送审时，草稿可按子请求终点最终采用。父范围继续显示待正式送审，补关联根审核的实际回执和读回后由主审沿同一采用入口确认。

原意见后作者已返修或作有据解释，主审读完整原意见、作者处理及当前正文，明确接受其适用于原用途时，可在原stage adopt用`--apply-review 原REVIEW_RETURN`及`--handling 原作者处理文件`补关系（均可重复）。仅原任务owner/sponsor可作此判断；程序核原独审身份与作者责任链、原评估结论、实际文件及当前准确对象/正文，并将主审判断另存原采用事件。原material_issue仍是原独审对其冻结稿的结论；有据处理和本次适用判断归当前主审，不改写成审阅者的新supported。未明确处理的问题继续阻断，not_assessed不能替代独立评估，新对象不借旧意见，后续正文或用途变化也不自动沿用。首次关系可按 stage 登记，也可在同一次 final 采用中明确引用。接手时用 `work-check --request REQUEST --for handoff --summary` 查看准确候选、意见和作者原件，以及每件和整包的待核状态。沿返回的 `check_ref` 读完整证据；保存、读回及授权按实际终点核实。

受管理的公共CLI调用将完整输入、stdout/stderr及本次新输出留在原run的`delivery/operation-*/`，由可信宿主关联原账本。补保存的后续turn复用原准确候选与各自已有意见、检查及保存读回；空coverage不会清掉文件关系。`--artifact`可重复，冻结所给准确文件，重复关联幂等；`--request … --result …`也可补文件。归档/索引故障只影响自动关联，保留原动作结果，沿原run重读或补原`record.json`，不重跑成功或未知写入。独立CLI没有run上下文仍正常使用。检查直接读原生完整报告，不要求lca-gate外壳；显示实际覆盖与notChecked，候选变化只影响相应成员的证据绑定。当前正文有差异时保留原意见和准确差异入口，由负责agent判断适用性。

Devin作者和独审保存普通报告并在final给准确路径；运行器自动保存原文、实际Session和run/return，主审沿原入口读回。它们的文件/命令权限不开放 `.control` 管理写入，无需再执行 `work-return` 或手写技术字段；科学范围覆盖与最终采用仍由原负责人按完整原件判断。

`work-complete`先留原说明再核当前全范围。未满足时保存completion-pending和具体差额，任务保持开放；补齐后同一文件可重试。成功、并发重试只转换一次；`work-reopen`后的新生命周期重新判断。普通完成不恢复暂停/取消，不解除未知数据效果。科学“范围已完整”的判断仍由主审对照原委派负责。

作者判断本次业务收尾有三类依据：达到合同终点并交清主审接手事项；合理且获准取证后达到有据证据边界，交可成立部分、排查依据、缺口和选择；剩余责任均受已核外部条件阻断，当前可做本地/离线部分已经完成，交依赖和恢复点。后两类保留未完成范围，由主审决定投入与接手。这是业务判断指引，不是报告类型gate，不要求无边界穷尽来源，也不以技术中断、预算或一次turn结束冒充科学完成。原Goal按实际状态保留。

主审及时接回并持续推进全部余项。当前计划就地维护有效目标/范围/授权、承接与跨包依赖、下一步投入及取舍；部分交付或依赖变化足以改变安排时替换相应段落并引用原证据。运行时点、重试和回执自动入原账本，退出当前指挥正文的旧决定仍可追溯；不要求固定标题、复盘表或另写恢复文件。

本工作区DSH模型推进为worker和reviewer各6席，总额12；原队列按角色分别FIFO，一类满额不阻挡另一类。原入口一次提交，满额自动等待并保持同一请求/Session；受控`review_result`等待让出worker席，回复模型前在worker池重新取席，原执行租约和stop保留。排队不算业务进度。用`harness.py work-capacity`只读核实际占用、队列、过渡旧进程与等待原因，取消/stop沿原run。准确运行边界见[宿主容量说明](../../docs/tools/production-host.md#dsh共享容量)。

## 原Session恢复与停止

```sh
.venv/bin/python bin/harness.py work-dispatch --request REQUEST --execute --continue --run-id ORIGINAL_RUN
```

普通dispatch/status/read-result只观察，不自动恢复。技术continue沿原brief和有效决定，核请求链、原生Session、进程及进程组；管理进程退出、空锁、无新输出都不单独证明执行已结束。原active Goal在获准技术接续时恢复原activation，保留目标、预算和历史；paused/complete/blocked按原事实处理。新增业务意见可用`--instructions 原说明.md`，与`--continue`互斥。

DSH原运输排队后退出时，`--read-result`若显示 `abandoned_before_start`，原负责人可用返回的 `--execute --continue --run-id` 命令取回原intent。程序再核原账本、完整输入和执行退出，按原序号、配置与目标Session接续。仍为 `launch_unresolved` 时沿原回执查证；有效stop继续约束原运行。

Devin在prompt前收到对应请求的明确拒绝、且没有本次工具请求时，读回可显示`failed/not_started`、原`target_session`和原响应；这与原Session仍被桌面占用同时成立。旧失败由公共readresult核完整ACP及执行退出后追加事实，原unknown保留。解决具体占用后，主审对该原run正常显式continue即可，不改库或另写恢复文件；原stop仍按明确恢复入口处理。桌面Inactive、切走或Archive没有证明释放，正式持有连接close与桌面最小交接条件见[Devin宿主](../../docs/tools/devin-host.md)。

原Session可读、原生写锁排除且完整OS归属clear时，可确认执行exited，即使历史turn缺结束事件；原native unknown和数据效果仍保留。明确continue只恢复该Session，不重放原工具或数据请求。DSH耗尽额度后的投入及complete后的新工作，作者在首个恢复turn按[正式Goal步骤](../../roles/data.md)落实，原生driver承担自动后续轮次。

明确停止后，只有原恢复决定和`--instructions 原决定.md --resume-stopped`可接续；`work-pause/work-resume/work-cancel/work-reopen/work-end`各保留原事实。运输unknown与数据写入unknown分开：未知准确对象/动作只沿原attempt核效果，不重复发送成功或未知写入；读件、报告和不受影响工作继续。详见[原生运行](../../docs/tools/production-host.md)。新技术材料自动落原program内`runs/<run-id>/`、`reviews/<review-id>/`及`checks/<fingerprint>/`，旧路径原件不搬家。

## 维护发现的正常接续

科学输入及本次用法由原作者修；共享缺陷沿原board交维护，给原错误、版本、受影响动作及恢复点。作者使用`report_finding`提交包内原报告，或`work-finding --task TASK --report 原发现.md`；续报给原finding ID。宿主记录真实身份，原报告不因读取格式限制被丢弃，独立工作继续。

```sh
.venv/bin/python bin/harness.py work-maintenance --limit 20 --offset 0
.venv/bin/python bin/harness.py work-maintenance-note --request FINDING --note 修复记录.md --status fixed
```

维护者沿分页读完整board、原finding及准确运行原件，亲自复现、修共享能力并核原后续动作。fixed表示本地已修，原负责人真实回验后才记verified；deferred/open/duplicate分别保存明确原决定。新观察不被旧结案覆盖。跨聊天通知按用户当次明确指示执行；不靠Hook唤醒、调度外循环或新定时任务推进业务。

工程交回后，原主审核具体依赖并接续原余项。旧审核get、新submit、审核退回、正文恢复及Flow保存各按自身输入、权限和当前回执判断；一个动作失败只影响已证明受影响的动作，干跑通过仍需实际动作与完整读回。身份恢复仅证明当次身份，不结清历史凭据生命周期问题。board记录本身不唤醒blocked业务；通知或恢复按用户明确安排执行。

## 历史记录

原任务、Session、Goal、attempt、成果、授权、停止及未知效果保留。旧Desktop信箱、handoff与流程状态机退出执行入口；旧表用`harness.py history --table TABLE --id ID`只读查询，当前任务完整事件用`work-show --id TASK --detail`。工具版本或管理格式变化不撤销既有科学事实与采用。
