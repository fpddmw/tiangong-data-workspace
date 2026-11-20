# 原生运行与原尝试恢复

当前运行入口是 `harness.py work-dispatch` / `run_worker.py`。实现状态和未完成验证见[唯一重构计划](../workspace/agent-autonomy-plan.md)。业务职责见[日常入口](../../workflows/business-flow/README.md)。

## 启动和观察

新包从 `.control/runtime.json` 的 `programmatic_worker` 选择已安装配置，明确的 `--config` 可指定本次配置。已有运行保留当时配置作为历史；下一次明确接续选择当前配置，保留原 Session 和宿主账号。本次配置决定实际模型、工具与明确写域，brief 的目录不授予文件权限。所有 agent 使用工作区根目录，启动和接续输入列出 AGENTS、职责、当前任务计划、原 brief 及所选原件路径，由 agent 直接读取；历史brief与本次有效目标的关系由agent据原文判断。

```sh
.venv/bin/python bin/harness.py work-dispatch --request ID --execute
.venv/bin/python bin/run_worker.py --request ID --status
.venv/bin/python bin/run_worker.py --request ID --read-result
.venv/bin/python bin/harness.py work-packages
```

一次派发执行一个原生调用，DSH／Codex Goal 由各自宿主完成连续回合。重复派发观察原运行，不新增作者。宿主保存实际 Session、turn、完整原文和技术回执，再做附属索引；索引异常保留原报告，重新读回即可补登记，不要求模型重表达。

作者明确调用 `request_review` 才启动独审。审阅使用独立只读 Session，同家族允许；作者在原 Goal 中收意见并返修。`review_result`默认立即观察；显式`wait_seconds`为0–600秒，可取消本次等待并保留同一review_id。每次取件核原生和已落盘事实，安静不等于死亡。普通final、JSON字段和主审采用不启动业务外循环。

返修可用 `prior_review_id` 绑定同一委派、对象和已返回意见。只需撤下一个未开始旧审时，用 `supersedes_review_id` 点名。原队列先保管新版再核旧审能否撤换。等待、请求返回和采用均沿原证据入口。 [作者与独审](../../workflows/business-flow/README.md#作者与独审)

观察时分别读取作者与各独审的原 run。作者原生调用结束即保存其终态和原文，不join尚未结束的独审；独审由独立OS作业持有原执行责任，作者调用结束不取消审阅。晚到意见保存在独审原run，新作者调用凭原review_id取件。部分意见保持可读，失败或中断不计完整科学审阅；主审据实际差额接续作者。

## DSH共享容量

本工作区harness管理的DSH模型推进分为worker（内部`author`）6席、reviewer（内部`review`）6席，总上限12。沿原队列按角色分别FIFO放行，一类满额不阻挡另一类；空闲席不跨角色借用。`work-dispatch`、同Session恢复及独立review_job均一次提交，满额在公共运行层等待，有空位自动继续原请求。排队、查询和空闲历史Session不占推进席；Devin不占DSH席。

```sh
.venv/bin/python bin/harness.py work-capacity
```

该只读查询列全工作区实际占用、排队、等待原因与准确原run。旧账本running/unknown不自动占位；启动意图、实际DSH进程及工具子孙按原执行归属计数。管理进程退出不单独释放仍活跃的执行。某角色达到6则该类新增等待，总占用达到12则全部新增等待；既有执行超额时保留原占位、等自然回落。

`occupancy_counts`分列已确认执行、启动预留与归属未知预留；三者均计入各角色6及总额12，未知预留不宣称真实active。同一原Session的排队、预留与多轮历史run只计一次。只读原生观察使用共享锁，容量用同一共享非阻塞探测识别原生排他writer；并发读者不会互相制造writer，不删锁或放宽真实执行互斥。

受控`review_result`等待释放worker推进席，原Session、请求/执行租约、writer保护及stop保持。工具回复交还模型前，原run以新的队列序号在worker池重新取席；`waiting_review`列出可核的让席事实。管理者或标记无法核实则仍保守占位。6位worker可各自等独审并重新取席，reviewer使用独立的6席上限。

旧队列切换由维护者沿明确原授权执行`work-capacity --adopt-policy --source FILE`，可先加`--dry-run`。同一容量锁内，只结束已经严格证明仍是原intent、没有本次native执行/child、原输入完整且管理身份准确的旧排队运输，保存原前像并接回同一run和序号；未放行的resume还须核准确原Session、前驱及原接续输入。已启动或未知执行拒绝接管。崩溃后沿原运输回执和票据恢复，不重派科学任务。普通查询没有策略切换副作用。

已加载四席旧代码的原进程继续使用原管理器。旧作者完成受控等待并排队重取时，新管理器先放行更早的票据，暂缓后到的票据，直到旧作者按原总额4和原全局FIFO取席。`legacy_reentry_draining`表示等待这次过渡。原管理器、执行组、原生writer及工具后代均已退出的等待票据由新管理器回收，原运行结果、停止和数据效果仍沿各自证据。已有管理器仍加载旧接入代码时，维护者可按明确授权执行 `work-capacity --retire-exited-waits --source FILE`，并用 `--dry-run` 先核范围。此动作只释放已证明无执行者的等待票据。`policy`显示当前采用策略，`protocol_policy`显示实际协议标记。最后一张旧票据退出后，原容量锁内退役旧标记。

获准的 provider 可在空窗切换为总额 4、两类各最多 3 席，按最早可入场票选择。自动接续也随这个 provider 启用，限已确认退出、有新工具成果、仍有预算的原作者 Goal。运输间歇失败从原终态观察和计数，触发冷却只限制自动接续。原权限、工具和数据效果沿各自回执。 [切换、排空与回验](../../installation/maintenance-20261116-noon/continuation-handoff/implementation/dsh-workflow-20261119/README.md#复跑与原队列切换)

原run的`readresult`／`--read-result`及作者`review_result`也直接显示同一队列的`queued`和等待原因。首次启动排队尚无native Session，同Session恢复排队沿原目标Session保留请求。若排队进程已退出，且原排队事件、配置、输入与原执行证据共同证明从未放行，读回显示 `abandoned_before_start`、原序号和原负责人续行命令。负责人明确对该原run执行 `--execute --continue` 后，原intent以原序号、配置、输入及目标Session进入原队列。证据不足时仍显示 `launch_unresolved`，沿原启动证据查证。新独审继承原作者请求在账本中当前已明确的范围关联，冻结候选和问题保持原件。

排队时原stop/取消仍生效，放行后重核原授权和stop；等待不持SQLite写事务，不被记为已经执行或失败。取消保存未启动事实，不冒充原生abort。队列仅保管技术等待/预留，原事实仍在run事件和execution.json；不增加业务状态或科学验收字段。该限制只覆盖本工作区harness入口，不代表远端服务所有客户端总上限。

## 同一 Session 接续

主审自身恢复会话时，直接读原委派、已有范围清单和当前计划，核清已覆盖及全部未达终点责任，包括已派工未交付、有返回待采用、未派和待核项，再决定本次作者接续或其他投入。某位作者的接续文件只说明其科学工作体；`work-show/work-packages/context`共用全范围投影，全部已经挂包不等于没有余项。当前计划随实质覆盖、依赖和投入变化就地更新，原判断的保管方式见[主审职责](../../roles/task-lead.md)。

原生调用已结束而原授权内仍有工作时，主审读实际回执，对照原目标把完整剩余交付及所需新增输入写在原包中，然后接续原作者。接续说明指向一份当前计划，写清本次目标及被替代的旧要求；已登记的原 brief 和回执保留历史版本。启动输入标明工作区入口、本职指南、原请求、本次接续说明和上一运行回执的路径，agent 直接读原文判断，程序不抽取正文或推导业务指令。worker 在原 Goal 中自主推进到科学终点；主审继续协调其他包及准备平台送审：

```sh
.venv/bin/python bin/harness.py work-dispatch --request ID --execute --instructions 原接续说明.md
```

原说明是普通文本，没有恢复表单。程序复用准确原 Session；相同前序attempt与相同说明的重复调用读取已有接续结果，后来明确选择新的前序attempt时可复用同一说明。本次说明可独立读取，旧brief或旧配置缺失不使已读成果失效。在途调用不能重复启动。仅需技术接续时沿原brief和有效决定执行，不另写恢复文件：

```sh
.venv/bin/python bin/harness.py work-dispatch --request ID --execute --continue --run-id RUN_ID
```

`--continue`与新增业务`--instructions`、stop互斥。两种明确接续都核原请求链、Session及执行归属：管理进程退出、空锁、无新输出或缺新格式均不单独证明旧执行已退出；仍有匹配原执行/进程组时只观察。确认执行退出、原生无活跃执行且Session可读后，才能在原Session继续。无法核实时给准确缺口，沿原回执、OS归属和原生事实查证；存量缺新元数据不能永远卡死，也不能伪造退出。历史UNKNOWN和停止事实保留，成功或未知数据动作只读回原attempt，不能因运输恢复重放。原Session身份确实缺失时返回依据，不创建替代作者。

**会话接续与 Goal 激活分别核实。** DSH原生`goal.phase`是持久状态，`activation`描述当前实例能否自动推进。获准技术接续时，宿主对仍active且disarmed的原Goal调用正式resume能力，保留ID、目标、预算和历史；已armed直接工作。作者用`get_goal`核实际状态，交付后按原生工具语义结束Goal。paused、complete或blocked不由harness改成active，明确新决定沿原生权限处理；普通文字不能激活Goal。Codex使用自己的原生Goal工具和宿主自动turn。具体用法见[本职指南](../../roles/data.md)。

DSH共同动态context在恢复首轮和实际压缩后提供当前phase/activation/已用轮次/上限及原指令入口。耗尽后先edit上限再用新revision resume；complete后的明确新目标在同一Session create。程序不从文字猜预算，不另加prompt循环。已证明OS退出但缺历史turn/end的原调用可以明确同Session接续，native/data unknown分别保留；原Session不可读、子孙活跃、PID复用或归属无法核清仍给准确限制。

`work-show/work-packages`和短Hook共用原独审可取事实，`review_result`完整返回才另记实际接收Session及原意见hash；路径提示不等于投递，投递不代替作者处理或主审采用。公共CLI完整证据留原run目录，可信宿主补关联；worker无需写管理库。自动留档失败不改变命令退出结果，也不重放数据动作；`work-return --artifact FILE`沿同一证据集合补原件。

DSH 工程诊断时可在选定宿主配置给 `sse_diagnostic_origin` 指定原 provider 的准确 origin。选定调用的 SDK 实际报告 SSE JSON 解码错误时，宿主在原 run 的 `diagnostics` 保存脱敏完整失败事件及边界摘要。诊断中的正常事件仅保留计数和摘要。原 Session 工具记录继续用于区分原生调用与助手字面文本。[安装版验证和观察边界](../../installation/maintenance-20261116-noon/continuation-handoff/implementation/board-capabilities-20261117/E-isolated-return.md)

遇到中断，先读原 run 的原生错误、该 turn 已失败的响应次数、工具结果和对应数据回执。一次超时可能已经由宿主重试多次。原执行退出、效果核清且原 Goal 仍有在授权工作时，负责人用上面的 `--continue --run-id` 接回原 Session。已经发出的独审沿原请求取意见，已经保存的对象按准确版本读回，仍未知的写入沿原 attempt 核实。若响应正常结束但交接文字失真，直接从原工作文件、独审及读回采用已成立的成果；只有具体未完成的实质工作才值得再次投入。对反复失真的同一响应路线，用原生 response ID 对照服务端入口及返回。原 run 与正常请求的原件留作对照。

```sh
.venv/bin/python bin/run_worker.py --request ID --run-id RUN_ID --stop '明确停止依据'
.venv/bin/python bin/harness.py work-dispatch --request ID --run-id RUN_ID --execute --instructions 明确恢复说明.md --resume-stopped
```

带停止标记的原运行，确认其原生终态后可按新的实际恢复授权接续原 Session；已成功结束的调用保留成功事实，不必改写为停止。请求停止与已经停止分别记录。作者与reviewer分别记录停止和结果。作者退出不结束独审；需要结束审阅时明确操作其原run。

主审此前以 `work-end` 结束委派、现在明确继续同一工作时，也使用上述 `--instructions … --resume-stopped`。程序记录主审的新恢复决定，保留原结束记录并接续原 Session。整个任务曾被过早标为 completed 时，主审先以 `harness.py work-reopen --task ID --note 原继续决定.md` 纠正原任务，再按实际未完范围接续委派；paused 任务使用 `work-resume`。这几项动作表达负责人的当前决定，原科学范围和数据授权沿原件核实。

确认模型尚未启动的首次宿主拒绝，可在修复后从原入口重试一次；作者接续若在启动前被拒绝，修复后重用原 `--instructions` 命令，原说明仍会交给同一作者。普通重复派发只观察这次失败。reviewer 的已知失败由负责agent读取原诊断后明确决定接续。未知效果、作者可写失败和用户停止不自动重放。正常传输保留原调用final及同一回合的原生Goal观察，调用成功与科学完成分别记录。丢失传输后的冷读只能取得持久Goal状态，无法确认进程内activation；准确最新回合结束但Goal仍active时，保留不确定事实，由原负责人核原尝试并明确接续。更晚回合或未知状态同样不被冷读改成整个调用完成；已读到的原turn正文仍返回供agent使用。

Devin的明确启动拒绝以原请求method/id、真实响应及prompt前阶段核实，不由错误码或retryable字段单独决定。完整原ACP若证明load拒绝且无本次工具请求，公共readresult可为旧run追加`not_started`并保留原unknown；`target_session`来自原launch，接续从该拒绝run沿原parent取得同一Session。真正prompt后断连、未退出子孙及未知数据写入仍保持保护。原生占用另由持有方正常释放，桌面归档不作为解锁依据，准确操作见[Devin指南](devin-host.md)。

DSH 会话的历史 cwd 由原生存储记录。2026-11-11 已将 NG/CU4/LS/SCM 四个原会话一次性承接到正式工作区，ID、全部事件和 Goal 历史不变；原文件及核验见原维护包 session-continuation。正常启动不生成旧目录；目录不符会在派发前给出准确诊断。子进程错误保留退出码、原生来源及完整安全诊断入口；短预览不参与分类，也不能冒充全文。

## 宿主能力与观察边界

Devin/SWE-2可由原派发入口按请求显式选用，[准确配置、同Session恢复及权限边界](devin-host.md)见宿主说明。它复用原任务/合同/成果与共享审阅服务，当前没有已验证的Goal API；Devin执行不占DSH席，选用DSH独审时仍遵守原独审配额。不同宿主保留各自真实Session身份，不能把迁移业务工作当作跨宿主恢复原Session。

DSH 作者支持原生Goal及调用内审阅工具；Codex程序化作者已开启原生Goals，运行器观察宿主自动turn，单次派发仅发送一次 `turn/start`。新建程序化Codex作者通过原生动态工具获得与DSH相同的调用内审阅能力，原线程接续保留该工具集。创建时没有协作工具的旧线程直接用[公共审阅入口](../../workflows/business-flow/README.md#作者与独审)继续，无需新开作者。Claude当前只有原生Session调用与显式接续，未提供已验证的自主Goal；需要连续Goal的包选择DSH或Codex，不把Claude普通final当作持续交付。

DSH本地启动插件通过正常Agent工厂创建或接续原Session，提交一次明确输入，按原生Agent／Goal事件等待；原生Goal驱动后续轮次。写域策略正常注册为原生沙箱服务，Bash和文件工具使用同一明确写域，Session cwd保持正式目录。冷读先保留准确原turn正文，再独立读取可选Goal投影；缺少Goal解释不使原文消失。Bash退出码不可观察时记录为未知，工具错误不触发作者重启。适配器依赖当前DSH公开服务接口，由维护者在本地随正式版本核验；没有whenIdle／resolve运行时修改，也没有业务外循环。DSH不在天工系列的上游issue授权范围内。模型与网络传输仍由宿主处理，不按错误字符串增补重试名单。

短Hook提供职责、完整计数和原件入口，8KiB只限制呈现，超长信息按整块转详情。Codex程序化启动参数启用hooks；新增或改过的具体定义仍须通过`/hooks`或Settings > Hooks审阅并信任，配置被发现不代表实际运行。DSH使用原生动态context，压缩后由原生projection恢复。项目自动指令入口保持在32KiB内；超限时关闭前缀加载，明确从原路径读完整入口和所指职责，不把前缀当已读全文。各层实际资格见唯一计划及产物。

## 工具环境

worker 通过项目公共 CLI 和 Skill 工作。具备native_cli的作者通过`tiangong_cli`提交`{role,surface,argv}`，角色组合、固定launcher和完整输出读取见[工具入口](../../LOCAL_TOOLS.md#同一公共入口)。DSH控制通道、Devin MCP与Codex动态工具共用执行实现，不按shell写法选择权限。工具接收与CLI业务成功分开，原run保存完整输出及原生操作归档；中断保留原attempt及未知效果，不自动重试。账号刷新仍由`refresh_editor_identity`经原CLI执行，凭据仍属原账号；独审仅使用`local_check`做离线检查，不修改候选或取得在线入口。

DSH 图片额度由原生组件维持，保留原始事件和文件，移出较早图片的模型上下文；不能靠反复开新作者绕过限额。配置、加载版本、本地测试及业务回验的实际证据分别记录在原安装包和维护 finding。

旧 `production.py` 服务、原 Desktop relay、工具禁用文本拼接、角色目录以及旧交付 schema 均退出当前入口。历史记录按[只读查询](../../workflows/business-flow/README.md#历史记录)保留。
