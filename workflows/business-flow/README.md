# 完整任务的日常入口

总主持续安排父目标、投入和跨包依赖；主审组织整包交付、用途采用及获准平台提交；作者在原Session/Goal内自主制作、自查、科学独审和返修；独审核准确候选与用途，原文直接交作者；维护者亲自修公共能力。所有人在同一正式工作区读原件。程序保管身份、哈希、关联和实际运行事实，并在相应动作边界集中检查。

## 从原范围接续

[总主](../../roles/global-main.md#委派与持续交付)从原建设计划判断本版覆盖和下一笔投入，队列清空时也从这里接续。[主审](../../roles/task-lead.md)从原委派、完整 brief、合同和整包当前计划接回这一批成果。`work-show --id TASK`、`work-scope --task TASK`、`work-packages --task TASK` 及 `context.py --task TASK` 共用已登记范围的完整视图，可核待确认的关联、未派、已承担未交付和有返回待采用的责任。

五态分别是未安排、已承担待交付、已验收、本方已提交、原委托方已处置。关系未核显示unknown，不预填完成。承接、作者明确返回覆盖、主审采用和最新已观察执行分别看；“已承担”不表示仍运行，局部返回不外推全request。历史采用未注明stage/final时保留原意义，显示验收层级待核。

Hook 按启动、恢复、压缩及重要事实变化提供短职责卡、完整计数和准确入口。`work-show` 的 `current_plan` 打开当前接续页，`commitment.frozen_ref` 按哈希打开已接受的承诺。Hook 分别给出两处地址。结论与对象、版本、用途及限制一起读原件。`work-show --history` 分页取原事件，`--detail` 展开原任务记录，`--wave` 读取波末截止点。`work-show/work-packages/work-scope/context --output /ABS/FILE.json` 可保存查询结果。

建包时冻结原目标。负责人确认原用途、验收终点、范围、决定性接口或授权投入改变时，用 `work-revise --task TASK --base-sha256 HASH --note 新基线.md --reason '原决定的理由'`。已有决定文件时用 `--reason-file 原决定.md`。受影响项可以重复给 `--affected-scope` 或 `--affected-request`。HASH 取当前实际读过的基线。日常进展从原账本读取。

## 登记范围与一份合同

[主审](../../roles/task-lead.md)确定要制作的对象、用途和验收终点后，用 `work-request` 和 `work-dispatch` 派发制作包。需要调研的部分可并行使用主审已获授权的原生子智能体协助定计划。

```sh
.venv/bin/python bin/harness.py work-create --brief 原目标.md --owner 主审THREAD_ID --contract 合同.json
.venv/bin/python bin/harness.py work-scope --task TASK --apply 范围与关联.json --dry-run
.venv/bin/python bin/harness.py work-scope --task TASK --apply 范围与关联.json
.venv/bin/python bin/harness.py work-request --task TASK --brief 完整子包.md --contract 子包合同.json --scope SCOPE_ID
.venv/bin/python bin/harness.py work-check --request REQUEST --for dispatch
.venv/bin/python bin/harness.py work-dispatch --request REQUEST --execute
```

合同集中写 `purpose`、`endpoint`、`scope_boundary.in_scope/out_of_scope`、`acceptance.text/refs`、`authority_refs` 及 `interfaces.text/refs`。子委派沿用请求绑定时的父合同，只需写本子包的具体用途、终点或其他差异。父范围的排除边界继续有效。完整 brief 承载产品或参考基准、证据、已有资产和背景。作者选择方法、工具与迭代。原文件引用经工具保存哈希。执行仍核当时的账号、对象与动作权限。

本次请求可用 `work-dispatch --config 配置路径 --execute` 明确选择宿主；[Devin/SWE-2配置与接续](../../docs/tools/devin-host.md)复用同一任务、合同、范围、原成果和返回。Devin与DSH各保有真实Session，跨宿主接手用同任务的新明确委派，先查原在途/停止/未知尝试；不把新宿主伪装成原Session恢复。全局DSH绑定不因单次选择改变。

终点为`evidence | candidate | saved_draft | submitted | published`：分别是可验收证据、本地候选、合格草稿保存及完整读回、本方正式提交、实际发布。父任务与子包可有不同终点；作者到达子终点后，主审继续父目标。研究或本地任务不强制保存；提交不等于专家通过或发布。

派包按[四种边界及原字段](../../roles/COORDINATION.md#用途与四种边界)表达使用者、用途增益、本版承诺、本包负责、物理/计量接口、已含背景、已有适配和待承接责任。共享供方沿原件建设一次，各消费者用原scope/source_refs及interfaces保留需求、准确版本和本方采用；跨Task不能把别人的request挂成本任务承接。全部子包final也不能自动缩减父scope。

**工程例子。** 父包承诺干基粉体供给，原scope分别有“煅烧模块证据”和“成品粉磨/供给”；给作者的子合同可写`purpose`为窑门煅烧模块、`endpoint:evidence`，interfaces交原料含水、每吨窑门产品、已含燃料及外部粉磨需求。作者只覆盖前项的返回可由主审最终采用；后项尚未承接时父包仍未完成。若父包已承诺干燥的燃烧排放，子合同改称“有限能耗”不会自动移除该责任。以上是接口/责任示例，不是新增生产委派、科学参数或统一必填项。

范围apply包含`items:[{scope_id,kind,label,source_refs,target_endpoint?}]`、`links:[{request_id,scope_ids}]`、`verified_scope_ids:[]`和`source_ref`。kind仅requirement/deliverable。提交为增量upsert，不按缺席项删除已登记范围。只有逐项核清原承接与覆盖关系后才登记verified；范围缩减、取消或替换仍需原委托方明确决定。历史未结构化项保留出处，不能猜成未派或完成。

`work-contract --task TASK`或`--request REQUEST`查看；原委派者/有权负责人用`--set 合同.json --source 原依据`修订。草案可先保存缺项，首次新科学派发集中指出缺口。历史原Session读件与技术接续沿原brief和有效决定，不以补新格式为前提；真正扩展范围先更新合同。`work-enroll --role ROLE --authorization 原任命`只登记本次真实任命，不转移原主审、写责或凭据。

## 派工后的等待

主审把具备开工条件的一批工作集中派出。活跃时处理能推进的待办，及时接回提前返回的成果，按需要采用、安排返修或续派。

用 `work-show --mine` 查看原委派的全部范围、剩余终点和原作者状态，再用 `work-show --id TASK --scope SCOPE_ID` 查某项的原尝试、已确认覆盖的回件和等待依据。跨项旧单中覆盖待核的回件按原请求归组，用 `work-show --id REQUEST` 逐件读。从 `original_brief_ref` 核委派用途，从 `current_plan_path` 进入建模者使用说明。波末或首次转入纯等待时，对照已采用分支的用途限制，再决定还有哪项可独立推进。新委派按请求显示；同一范围有待核原尝试时，打开提示的 run 和只读派工预览，核对实际对象、原写者及写入效果。作者实际席位从对应宿主的 `work-capacity` 读取；有独立可办的材料时由主审续派。

**只剩等待时，一次交给宿主连续等待 30 分钟。** 用 `work-show --id TASK --wait-seconds 1800 --after CURSOR` 等本人事项。多包可重复给 `--id`，或用 `--mine`。有事可做就处理，提前返回就接回、采用或续派。宿主的单次等待会在有待办时返回，安静期间由宿主等满 30 分钟。游标记录展示，处置或有效暂缓才结束待办。普通超时沿已核过的依赖接续，发生实质变化时再判断受影响的工作。

需要暂缓一件时，用 `work-wait --task TASK --item ITEM` 引用 `work-show` 返回的待办，附 `--for-request REQUEST`、`--for-file 原件` 或 `--until 带时区的时刻`。已核实的外部依赖可加 `--external` 并引用其原件。条件变化时只恢复受影响的待办。原委派的合同、输入和宿主就绪时，`work-show` 直接指向派工。波末用 `work-wave --task TASK --decision 原继续或收束决定ID` 记录当时事实。每两波提醒原总主复核，异常提前。总主处置可在 `work-dispose` 引用 `--wave 书签ID`。

## 作者与独审

独审意见交作者；主审采用后交给使用者；改变原授权的取舍交委托方。需要参考写法时，见[不同证据条件下的交接](../dataset-review/reply-templates/交接示例.md)。

作者按合同核库取证、制作、自查，并用`request_review`给出准确候选路径与科学问题。有原生Goal的宿主自行维护Goal；Devin按原brief和合同在持久Session的一次调用内连续推进，当前没有已验证的Goal API。宿主冻结候选、保留实际独审身份，启动独立只读OS作业；作者调用结束不取消独审。同模型家族允许，不能复用作者Session自审。

新作者请求交一项能够独立验收的责任。同一单元确需交接时，沿 `work-request --continues REQUEST` 关联原范围、回件和已有轮次，实际接续仍核原尝试、停止和数据效果。多材料旧单的各项若可分别验收，用 `work-show --id REQUEST` 查整包余项及重叠请求；主审核清旧写者和迟到意见后沿相应单元继续。不同用途从原任务另行委派。已有储备的任务在父合同 `reserve` 写明 `scope_ids`、`core_scope_ids` 和 `max_requests`。核心达到终点或仅依赖已核实的外部条件时，按原授权投放已就绪的储备。

同一委派和对象的返修可通过 `prior_review_id` 指定已返回的原独审。独审可直接读取原意见、两次冻结稿、差异和已知消费者。另用 `supersedes_review_id` 指定同一作者的一个未开始旧审。新版登记并冻结后，原队列核准撤换。已领取、已完成或归属待核的旧审返回其实际状态；作者可继续原工序或读取原审。

同一对象和用途的可确认前审自动带入，原说明含可唯一核对的原审号时也能定位。有歧义时提供原件供作者和独审选择。两个审阅入口共用三轮自主预算，完整独立意见消耗该轮。追加时，主审用 `work-decision-record --review 原作者请求或审号 --add 1 --reason '补审改后的干基分母'`。同次审阅涵盖多个已用尽预算的对象时，按入口返回的准确对象加 `--object`。作者沿原冻结请求接续。

`review_result`默认立即观察，显式`wait_seconds`可选0–600秒并可取消。取件先核原生/已落盘事实，保留原review_id；安静不等于死亡。作者继续独立工作，意见到达后返修、复核或有据解释分歧。部分意见可读，失败或中断不冒充完整审阅。

其他宿主或缺少便捷工具的旧线程使用同一公开入口，不换作者、不重做成果：

```sh
.venv/bin/python bin/harness.py work-request --task TASK --review-of AUTHOR_REQUEST --brief 科学问题与冻结候选.md --contract 审阅合同.json
.venv/bin/python bin/harness.py work-dispatch --request REVIEW_REQUEST --execute
.venv/bin/python bin/run_worker.py --request REVIEW_REQUEST --read-result
.venv/bin/python bin/harness.py work-review-record --return-id REVIEW_RETURN --conclusion supported_with_limits --basis 原意见.md
```

该审阅brief给出冻结候选及准确科学问题；合同沿上面的同一字段声明只读审阅范围和evidence终点。旧线程读件不要求补格式，新开审阅的首次派发仍使用同一集中检查。

独审和作者各在正常原件写一次，审阅入口保留原文、先前意见、作者处理和两版冻结候选。完整回件按原返回及相关执行事实计轮。已撤下的排队请求、已确认未评估、无意见的终止及失败按各自记录处理。`work-review-record` 供引用已明确的原结论。遇独立确认修好后真正复发并需主审裁决，可沿同一动作加 `--recurs-after EARLIER_RETURN` 并引用现有意见。其余复核和采用直接读原文。受限结论是否符合任务用途由主审决定。

完整复审原文明确解决并替代哪份原意见时，有原任务索引权限的负责人可在同一`work-review-record`加`--supersedes 原REVIEW_RETURN`（可重复），依据仍用该完整新意见；工具核同一作者责任链、先前独审及全部原对象身份被新冻结包覆盖。两份原文、结论及辅助报告均保留；纯文字原委派复用其已冻结索引，规范数据对象才定义此处的目标身份。取代只对新意见实际覆盖的当前正文生效。没有明确取代关系时不按时间新旧清掉material_issue；也不让已明确解决的问题永久阻断未变成员。索引不替代主审用途采用。

## 随时返回、采用与完成

DSH 和 Devin 的作者在原生 final 写交付结论、限制及工件路径，独审直接交完整意见。宿主用原 Session 和 run 自动登记回件，主审从 `work-show --id REQUEST` 接回。下面的 `work-return` 供有任务库写权的会话登记手工报告，或按原 run 补关联原件。

```sh
.venv/bin/python bin/harness.py work-return --request REQUEST --result 原报告.md
.venv/bin/python bin/harness.py work-return --run RUN
.venv/bin/python bin/harness.py work-return --run RUN --candidate /ABS/候选.json --artifact /ABS/原生回执.json
.venv/bin/python bin/harness.py work-dispose --return-id RETURN --use adopt --decision 用途与限制.md --scope SCOPE_ID
.venv/bin/python bin/harness.py work-dispose --return-id RETURN --use adopt --decision 原用途采用.md --scope SCOPE_ID --apply-review 原REVIEW_RETURN --handling 原作者处理.md
.venv/bin/python bin/harness.py work-check --request REQUEST --for handoff --summary
.venv/bin/python bin/harness.py work-dispose --return-id RETURN --use adopt --acceptance final --scope SCOPE_ID --decision 最终采用.md
.venv/bin/python bin/harness.py work-complete --task TASK --note 完成依据.md
```

阶段成果、局部报告、独审意见和失败随时可接回。普通原文即可交付，作者可自行标出覆盖。`adopt` 默认阶段采用。最终采用由主审用 `--acceptance final --scope` 选定范围，沿回件核准确正文和终点。作者自报覆盖留在原件。`--checks` 可引用本服务生成且指纹仍匹配的检查文件。原生工具仍核真实账号、所有权、前像、精确授权及写后读回。

有多个历史版本时，重复 `--candidate` 选定本次交付的完整准确候选集合。可提供规范正文或带完整正文的准确原生读回。`--artifact` 单独关联保存、独审和审核记录。后继版显示原意见和改动，新版科学采用依原作者处理和主审判断。父范围另需送审时，草稿可按子请求终点最终采用。同一采用的候选均有已核实的原生送审回执及读回后，父范围达到 `submitted`。下一岗从 `work-show` 的审核号、原作者请求和读回入口接件。[逐根送审](../dataset-review/送审准备与实际送审.md)

作者返修或作有据解释后，主审读完整原意见、作者处理和当前正文，用同一次 `work-dispose --apply-review 原REVIEW_RETURN --handling 原作者处理文件` 接回。程序核独立身份、作者责任链、原回件、冻结候选和原用途。普通原文可以直接引用，已有明确索引的结论和实质问题仍可追，`not_assessed` 保留其未评估状态。主审可按适用范围做阶段或最终采用。后续对象、正文或用途变化由负责人重新判断。用 `work-check --request REQUEST --for handoff --summary` 定位准确候选、意见和作者原件，沿 `check_ref` 读完整条件，按实际终点核保存、读回和授权。

受管理的公共CLI调用将完整输入、stdout/stderr及本次新输出留在原run的`delivery/operation-*/`，由可信宿主关联原账本。补保存的后续turn复用原准确候选与各自已有意见、检查及保存读回；空coverage不会清掉文件关系。`--artifact`可重复，冻结所给准确文件，重复关联幂等；`--request … --result …`也可补文件。归档/索引故障只影响自动关联，保留原动作结果，沿原run重读或补原`record.json`，不重跑成功或未知写入。独立CLI没有run上下文仍正常使用。检查直接读原生完整报告，不要求lca-gate外壳；显示实际覆盖与notChecked，候选变化只影响相应成员的证据绑定。当前正文有差异时保留原意见和准确差异入口，由负责agent判断适用性。

`work-complete`先留原说明再核当前全范围。未满足时保存completion-pending和具体差额，任务保持开放；补齐后同一文件可重试。成功、并发重试只转换一次；`work-reopen`后的新生命周期重新判断。普通完成不恢复暂停/取消，不解除未知数据效果。科学“范围已完整”的判断仍由主审对照原委派负责。

作者判断本次业务收尾有三类依据：达到合同终点并交清主审接手事项；合理且获准取证后达到有据证据边界，交可成立部分、排查依据、缺口和选择；剩余责任均受已核外部条件阻断，当前可做本地/离线部分已经完成，交依赖和恢复点。后两类保留未完成范围，由主审决定投入与接手。这是业务判断指引，不是报告类型gate，不要求无边界穷尽来源，也不以技术中断、预算或一次turn结束冒充科学完成。原Goal按实际状态保留。

主审及时接回并持续推进全部余项。当前计划就地维护有效目标/范围/授权、承接与跨包依赖、下一步投入及取舍；部分交付或依赖变化足以改变安排时替换相应段落并引用原证据。运行时点、重试和回执自动入原账本，退出当前指挥正文的旧决定仍可追溯；不要求固定标题、复盘表或另写恢复文件。

本工作区 DSH 和 Devin 两个宿主分别有 worker 和 reviewer 各 6 席，每个宿主总额 12；原队列按角色分别FIFO，一类满额不阻挡另一类。原入口一次提交，满额自动等待并保持同一请求/Session；受控`review_result`等待让出worker席，回复模型前在worker池重新取席，原执行租约和stop保留。排队不算业务进度。用`harness.py work-capacity --host dsh`或`--host devin`只读核对应宿主的实际占用、队列、过渡旧进程与等待原因，取消/stop沿原run。准确运行边界见[宿主容量说明](../../docs/tools/production-host.md#dsh共享容量)。

## 原Session恢复与停止

```sh
.venv/bin/python bin/harness.py work-dispatch --request REQUEST --execute --continue --run-id ORIGINAL_RUN
```

普通 dispatch/status/read-result 只观察。技术 `--continue` 沿原 brief 和有效决定，核请求链、原生 Session、进程及进程组。原 active Goal 在获准技术接续时恢复原 activation，保留目标、预算和历史。原单元余项可用 `--instructions 原说明.md` 接续。原单元已交清时，同一用途的明确返修可附 `--basis 原独审回件或返修决定ID`；新用途、新独立终点从原任务另派请求。

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
