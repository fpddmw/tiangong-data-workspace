# Process／Model 命名方法学本地试行

> 本页是迁入正式目录的后续维护版本；原文与已绑定历史回执保留原位。迁移不产生新的科学审核、运行成功或业务操作许可。
> [原始版本](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-methodology-trial-20260927/README.md)；来源 SHA-256：`3b5cd7294bd8bf0b13dfe56ee212fa716510afab266783a0b9bbd3ef6e7d5641`。

上级：[当前编辑者名称案例](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/README.md)。2026-09-27 用户要求先持续试用已讨论的四段命名更正；2026-09-29 进一步明确本地试行不因等待人类审阅而阻塞，后续任务继续使用，用户在线上看到具体问题后再反馈修订。不需要为每批名称重新取得人工签字。本文供本工作区后续由用户发起的 Process／Model 名称编写使用，具体任务的新指令优先。它是**本地试行补充**，不是已发布的 TIDAS methodology，也不改变当前安装的 Foundry、CLI、Skill 或线上 Schema。

试行依据：[四段内容与样例](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-implementation-20260926/four-field-methodology/四段命名方案与六条案例对照.md)、[保留既有组合和交付术语的 Spec PR #32](https://github.com/tiangong-lca/tidas-spec/pull/32)、[Issue #31](https://github.com/tiangong-lca/tidas-spec/issues/31)。电网事项采用比六条总表更新的[组合身份更正](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-implementation-20260926/four-field-methodology/electricity-mix-inheritance-20260926/电网组合名称更正.md)、[交付点统一](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-implementation-20260926/four-field-methodology/electricity-consumer-delivery-unification-20260926/电网交付点用语统一.md)和[电量等量衔接](../../reports/current-account-draft-consolidation/report-eee38b7203.md)。[已安装方法学原件](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/context-process/outputs/methodology.yaml)和原回执保持不变；草案与已发布规则不一致时，明确记为试行解释，不宣称已获正式方法学符合性。

修改本命名方法时，核对本页引用的用户决定和正式方法学，注明此次调整适用的对象及已有示例的变化。为某个候选命名时，直接按下节核该对象。

## 编写与判断

从完整 Process／Model 信息、精确版本的参考 Flow、参考交换、来源、技术、时间、地点和边界生成名称。旧名只用于修订时检查信息是否遗失，不能以旧名为唯一输入。中文、英文分别核对；同一语言每个字段只用一个文本节点。

| 字段 | 试行内容 |
|---|---|
| baseName | 产品或服务身份；单参考 Process 与**兼容的精确参考 Flow** 的该语言基础名对齐。共享 Flow 不要求其他三段相同。多参考对象按其实际功能另行审阅。 |
| treatmentStandardsRoutes | 有依据的等级、处理标准、原料或具有辨识意义的工艺路线；不填“生产路线”等占位词。 |
| mixAndLocationTypes | 有依据的生产组合、消费组合或特定技术，以及产品可取得的交付节点；组合身份与交付节点分别判断。保留习惯术语，不凭交付地点推断市场份额或供电组合。 |
| functionalUnitFlowProperties | 影响产品功能或替代性的、有证据和基准的定量规格，例如适用电压；不写参考交换量、1 kWh = 3.6 MJ、足迹结果或无依据的供应份额。 |

“特定技术”仅在确有依据且有助于区分第三段口径时使用，不能冒充生产／消费组合；工艺路线已在第二段说明清楚时，第三段可以只写有依据的交付节点。省级生产／消费组合可在交付节点后注明省份，年份仍由时间字段承载。

电力名称的产品词跟随经核实的参考 Flow，不全局把“电力”换成“交流电”。发电端的生产组合、用户端的消费组合应保留其真实或明确继承且有依据的身份；一进一出的接口不能凭自身证明上游组合构成。**已证实消费组合身份**的同一用户交付边界统一用“消费组合，到用户”／“Consumption mix, to consumers”；“到用户”只表达接口，不能暗示本层新增运输、变压设施或线损。电压接口如确为 1:1 模型对应，可用“35–330千伏与1–35千伏电量等量衔接”描述第二段；不能把它写成实测零损耗。Model 名称概括整个模型，resulting Process 继承经审阅的完整 Model 名称。

代理负责名称的语义审阅和必要调查。**没有用户逐条签字不是阻断理由。**能用来源或明确、可复核的专家判断支持的字段继续生成和验证；Flow 完整身份冲突、组合来源无证据、术语暗示未建模的物理环节或原生验证失败时，仅相关对象保留待查，其他对象继续。不要为绕过空字段限制而填套话，也不要把命名检查通过等同于科学完整或发布资格。

## 每个新任务如何采用

1. 先取得当前受控 context pack、完整对象及精确参考 Flow。把本试行规则、实际适用的电网更正和每字段证据写进该任务的 brief／名称候选；记录适用版本与任何同已发布方法学的差异。
2. Foundry 任务通过其**公开 brief／interaction** 登记采纳并核实回执；直接 CLI 案例在所属 case 保留提案、字段证据、原生验证与旧／新对照。单凭本文或聊天记录，不声称既有 Foundry 任务已自动采用。
3. 用当前受控工具验证 Schema、作者语义和必要引用；通过后按该任务已有的写入权限、单写者、当前前像和原生保存流程继续。试行本身不授予发布、公开数据改写或全库批量修改权限。当前合格 CLI 的 name-plan 尚未实现；历史工程分支的 name-plan 回执只能作为试验参考，不假装已安装。
4. 用户后来指出某条线上名称问题时，从精确对象和写后原件复核，修订相应候选和本文的可变指引；保留旧试验回执，不自动重写其他数据。

## 起始本地核验

[乙烷制乙烯的冻结命名候选](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-implementation-20260926/four-field-methodology/trials/shared-flow-ethane-route/materialize/named-payload.json)已由当前受控 CLI 的 dataset validate [复验为 1／1 有效](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-methodology-trial-20260927/ethane-sdk-validation/outputs/validation-report.json)；这是 Schema／作者层本地核验，不是线上保存。最新[两条电网交付点试验](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-implementation-20260926/four-field-methodology/electricity-consumer-delivery-unification-20260926/sample-results.json)仍因组合身份依据不足而待查，零个可保存载荷；[电量衔接试验](../../projects/data-library/runs/draft-consolidation-20260924/naming-review/local-implementation-20260926/four-field-methodology/voltage-equal-energy-link-20260926/sample-results.json)也保留同一科学限制。这些阻断不是缺少人类签字。本文建立试行入口，本轮未修改线上数据。
