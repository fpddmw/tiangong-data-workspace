# 原生审核、专家分配与发布的只读适配

本页供用户指定的[轮询维护者](../../roles/platform-feedback.md)定期读取平台意见、按包汇总给各主审。沿原平台记录取证。`review_capture.py` 导入本地脱敏导出，`review_adapter.py` 解析证据，`harness.py work-business-observe` 关联原任务。实际平台动作沿[送审入口](../dataset-review/送审准备与实际送审.md)由具备相应授权的操作者执行。

## 处理平台意见

1. **读同一原审核。** 使用获准账号取得本轮意见及变更，保留精确对象、`(review_id, reviewer_id)`、`modified_at`、内容指纹、来源时间和已有送审校验和。需取原响应时按[一次有界读取](#一次有界读取)执行。缺页、权限失败或矛盾读取保留采集缺口；采集完整而送审正文、资格或闭包不全时可先归集意见，待业务采用时补所需证据。
2. **按原任务汇总给主审。** 轮询维护者引用原页，把新增、变更和撤回按包汇总给对应主审。需要在原任务登记某项意见时，可用 `harness_feedback.py ingest-capture --payload ORIGINAL_IMPORT.json`，再用 `show --task TASK_ID` 查看已登记意见。输入含 `task_id`、`capture_ref`、`expected_project` 和 `expected_account_user_id`，合成输入标明 `offline:true`。重复导入复用原记录。
3. **接续具体业务。** 确需答复或返修时，由该项业务负责人对齐送审正文、当前线上完整正文和原计划／本地候选。用 `harness_feedback.py dispose --request FEEDBACK_REQUEST_ID --decision revise_data --note 原意见回应.md` 选 `revise_data`、`clarify_only`、`not_adopted_with_evidence` 或 `needs_clarification`，给依据与影响。例如专家指出旧分母时，先查该值是否属于当前申报正文。反证成立时记录不采纳理由及数据自身合格证据；需要改正文时发出准确集中返修。
4. **返修或结束。** 先取得原审核实际退回及可编辑状态读回，再沿原计划返修，复核受影响差异。等待退回时可完成只读诊断和本地预案。原意见的新版本和撤回保留原记录；作者根据实际变化判断需要复核的内容。专家意见、管理员批准、真实发布分别留证。以发布为终点时核本轮实际目标、必要依赖／替换、引用与系统验收；较窄终点按原 brief 交回。平台后续动作由有权的原操作者接续。

以下为原生读取、封装和历史实例，按当前缺口展开。

受托处理具体意见的负责人可以用普通原文记录回应。

```sh
.venv/bin/python bin/harness_feedback.py dispose --request FEEDBACK_REQUEST_ID --decision revise_data --note 原意见回应.md
```

动作可选 revise_data、clarify_only、not_adopted_with_evidence、needs_clarification；依据和问题写在原文，不填管理字段。可用 `--revision-request` 指向已有作者请求。回应已登记仅表示负责人的决定已保存；是否修好数据由作者和独审按原用途核实，程序不计算采用范围、不强制建立返修子任务。

## 一次有界读取

查看或汇总准确审核的可见意见时，可用正式 CLI 0.4.4 的 [review opinions](../../docs/tools/review-preflight.md)，按审核号或准确对象版本取件，保留角色范围、分页、提交校验和及意见摘要。账号看不到的部分明确记为缺口。需要完整角色、目标正文、关联范围或需要从原始回执导入任务时，按下面的步骤补证；CLI 的结果封装不是 `review_capture.py` 的输入。

1. 使用当前已登录账号，核服务器当前用户ID、项目和实际角色；Chrome昵称不是身份凭证。只读查看原任务对应的review和精确目标，核删除/私有排除范围。账号无审核权限时保留失败，不把空结果当作没有任务。
2. 用受支持入口读取本轮review、同单元所有comments、相关成员角色，以及必要的目标完整正文、正文校验和和Reference Review范围。原查询的筛选、分页、失败和观察时间一起保留。查询UI的评论摘要不能代替逐专家原生记录。
3. 浏览器可从Network导出明确筛选后的 **HAR (sanitized)**。保留必要的原生方法、URL、参数、状态、完整响应正文、请求时间和Range信息；认证头、cookies和无关浏览器数据不进入版本库。仅使用已经验证为读取的 `qry_*` RPC，`POST`在这些RPC中是只读查询的传输方法，不是允许调用写命令。
4. 使用下列导入及观察入口。导出是数据证据，不是授权；不因导入成功自动执行下一动作。所有输出使用新路径，原输入保持原位。

```sh
python3 bin/review_capture.py \
  --identity ORIGINAL/PACKAGE/identity.json \
  --scope ORIGINAL/PACKAGE/read-scope.json \
  --har ORIGINAL/PACKAGE/reads/reviews.har \
  --har ORIGINAL/PACKAGE/reads/comments.har \
  --output ORIGINAL/PACKAGE/native-capture.json
python3 bin/review_adapter.py ORIGINAL/PACKAGE/native-capture.json
```

新完整任务的意见读取回到[处理平台意见](#处理平台意见)第 2 步。

## 本地封装与原生事实

本地封装schema为 `data-workspace.native-review-capture.v1`，不是向Foundry/CLI提交的新原生合同。可直接核验的字典与源格式入口在[适配器](../../bin/review_adapter.py)、[导入器](../../bin/review_capture.py)及其反例测试。

| 部分 | 必须保留的含义 |
|---|---|
| profile / contract_profile | native或synthetic分别标识；source_v2与实际观测到的observed_v6分开。源码出现某版本不证明线上已部署 |
| source | 项目、当前用户ID、身份依据、带时区观察时间和读取来源；凭据不进入封装 |
| scope | 精确table/id/version、明确review_ids、原目标声明的必要closure_targets；不从相似名称猜对象 |
| responses | 原RPC名/参数、HTTP状态、error、原data行和采集完整性；paged响应必须保留页码/大小及原total_count |
| datasets | 独立完整读回的精确目标、state_code、json_ordered/json、观察时间；没有取到就留缺口 |

身份文件包含上述source字段和原 `qry_membership_get_mine` 的membership_response；范围文件与scope形状相同。导入器解释当前支持的只读响应，保留未能解释的原件指针和原因；这些解析规则不授予调用权限。忽略OPTIONS预检，实际采集核准确项目与账号，原始凭据不进入材料。文件按解析后的实际路径读取，保护凭据和原生状态。输入、合计HAR与输出有16MiB边界；重复JSON键、非有限数值、缺失/截断正文、未完分页或Range读取均不能伪装完整。

## 三种判断分别成立

**观察平台分配。** review state=1、reviewer_id中不同身份与同review的非撤销有效comment逐一匹配。临时选择的UUID列表不算完成分配；已关闭的comment也不能回用作当前待审分配。当前review-member/admin角色不是专业能力或利益冲突证明。这些记录说明平台当前分配事实，不给本方主审增加分配或审核裁决职责。

平台审核资格与裁决由平台有权者负责。作者的科学独审按本包用途核证据；读取平台成员角色不替代科学意见。

**审核意见与终审。** 原生意见身份是 `(review_id, reviewer_id)`，没有另外的opinion UUID/revision。使用modified_at、内容和整行指纹区分新/改意见；明确state=-2才是撤销，记录消失保持不确定。管理员最终关闭为2或-1会覆盖原状态，因此不能由closed=2推断专家原来同意。`--previous`只比较同账号、同项目、同范围、同profile且更早的完整读取；出现缺页、权限、错范围、重复读不一致等情况时不推进cursor。cursor仅为完整采集结果，不是周期任务或业务成功。

**实际发布或可编辑退回。** 当前已审源码中，管理员批准会同时把review改为2、目标改为100，并可能写入审核元数据，改变正文校验和。适配器分别核批准事实、批准后的校验和、精确目标100读回及声明范围；专家建议通过不能替代它们。批准后的正文不必等于送审前正文，但必须匹配原生approved校验和。拒绝review=-1不必使目标变成0；还要完整枚举同目标的其他活动审核并鲜读实际可编辑状态。Root审核完成不代表其Reference Reviews已发布。

校验和遵循绑定源码的JSONB规范算法，保留Unicode键顺序、数组顺序与Decimal精度。已经被普通浮点解析丢失精度的正文不会强行取得原生校验和资格。缺少原生submitted/approved校验和时如实显示证据缺口，不用本地自算值代填原生字段。

## 本轮真实验证与边界

[2026-10-29验证](../../materials/packages/four-agent-business-flow-20261029/adapters/README.md)先保留原Data Manager账号403的失败，然后用用户指定的Sara会话核实review-admin并读取同一试点的真实V6队列及三条待审核comment。导入后的事实与原响应逐项匹配。

2026-10-29 这次接口验证仅保存读取事实，未执行专家分配、审核决定、退回或发布。其操作边界属于该次历史验证；当前任务按自身具名授权推进。该次 V6 响应缺送审校验和及部分业务证据，所需后续取证由原任务负责，接口验证本身仅证明读取能力。

## 原任务的观察记录

用 `harness.py work-business-observe` 将原 capture 关联到当前任务。精确目标与原任务范围一致；native 与 synthetic 分开。程序保存原读取、缺口和时间，不建议或改变业务阶段，不把采集成功当作科学验收。历史 v1 看板留作原记录，不再提供写入或迁移路线。

## 追加当前CLI正文证据

`review_capture.py --process-get EXACT/PROCESS-GET.json`可附加官方CLI的精确Process读回。输入为当前 `dataset process get --id UUID --version EXACT --json > /ABS/PROCESS-GET.json` 的完整 stdout（此用途不用 `--output` 或 `--fields`）。导入器核标准结果信封、完成状态、effectiveInput 与 subject 的精确版本、项目、完整性、正文 UUID／版本及观察时间；缺正文、投影或 latest 读取不能充当精确完整证据。旧导入结果保留原事实。结果只标为完整当前`json`及本地摘要；没有`json_ordered`证据时不会获得native fingerprint资格。`qry_review_get_member_workload`与名单查询一样只提供当前角色/工作量，不能生成专业资格或COI通过结论。

观察事实区分`submitted_checksum_observation`的field_absent、reported_null及reported_value；`collection_reads`分别保留原HTTP状态与本地采集验证原因。缺少比较输入不叫正文不一致；采集缺页与数据证据缺口分别呈现。当前实例的[分类证据](../../materials/packages/four-agent-business-flow-20261029/adapters/qualification/README.md)保留原观察的未知；阶段判断由负责 agent 作出。
