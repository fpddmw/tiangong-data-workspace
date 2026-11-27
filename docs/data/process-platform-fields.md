# Process 年度量与说明字段

用于 Process 作者构造，以及普通编辑／Data Check／保存后的字段回验。依据当前 P2 v3；主流程见[候选准备](../../workflows/dataset-review/candidate-preparation.md#process-annual-quantity-and-platform-round-trip)。本页规定作者输入和验收，不实现 Platform 序列化或替代原生 SDK／CLI 校验。

## 先固定年度量含义

在原包的 `annual_quantity_basis_if_process` 证据中保留：年度数字、适用年度／期间、精确参考 Flow → 属性 → 单位链，以及实际生产量、固定专家假设或参考量回退的类别与依据。真实来源优先；保持已经确定的假设，不因换会话重新取数。

## 按实际路线选择年度表示

| 当前证据与路线 | 作者填写、工具输出和下一步 |
|---|---|
| 有适配的真实年度来源 | 把真实数字及其单位、地区、工艺与期间绑定到原证据。用合格原生入口构造年度字段，核完整往返及实际校验报告。 |
| 缺真实年量，原任务适用 P2 且能走合格 Platform 路线 | 选一项固定可复算的假设，或以参考流数量及其精确参考单位作年度替代量。按当前 SDK 数值文本格式填写年度字段（数字加单位或上下文），方法字段写出数字、单位、期间、推导及用途。实际产量保持未知，代理量按本次申报范围使用。 |
| 原 Task 或 process builder 的原生路线对缺证返回 `[]` | 保留 SDK／CLI 返回的 `annualSupplyOrProductionVolume` 及原作者证据缺口，原 Task 继续使用其绑定合同。若当前 P2 任务需要具体年度字段，先完成固定方法依据和可独立交付部分，再用真实合格的作者入口及相应独审／门禁完成字段。只有原生 `[]` 的候选保留年量缺证与发布、供方年度权重限制。 |

[项目默认 P2](../../work/control/data-principles/PRINCIPLES-v3.md#p2-专家判断与基准方案)管理科学假设及 Platform 交付。[锁定 Skill](../../.agents/skills/process-automated-builder/SKILL.md)说明其实际 CLI 的缺证输出。保留原始报告和任务特定限制。已有 Task 如适用项目原则，通过该 Task 的公开决定／交互入口绑定，核实际采用。若无兼容的原生路径，在原包记录 `HOLD_CAPABILITY` 和适用限制，把确切工具冲突交[维护者](../../workflows/business-flow/README.md#维护发现的正常接续)。

作者保留年度字段的数字、单位和上下文原文，在方法字段表达期间与可复算依据；参考单位链用于解释数值，不由编辑器替换年度字段。例如功能单位描述含“1 kg”时，年度数字仍直接使用所绑定的参考单位。按实际部署核生成方式和保存正文。真实零需实产证据；从功能单位自动推算、按次随机变化、`9999` 等值交回原报告及其影响。旧版本材料按[本版原授权与历史](../../work/control/data-principles/authorization-v3-20261030.json)查证。

## 字段分工

下表 `P = processDataSet`，`R = P.modellingAndValidation.dataSourcesTreatmentAndRepresentativeness`。原内容按科学含义分配，不按“哪里能存下来”任意移动。

| 内容 | 目标字段 | 完成条件 |
|---|---|---|
| 年度数字、单位与适用上下文 | `R.annualSupplyOrProductionVolume` | 年度数字与精确参考单位及证据对应；原生多语言文本满足 SDK 字段长度和语种要求 |
| 固定假设及参数 | `P.modellingAndValidation.LCIMethodAndAllocation.modellingConstants` | 保留数值、单位、假设身份和理由，不称为实测 |
| 年度基准、推导、换算、外推 | `R.dataTreatmentAndExtrapolationsPrinciples` | 明写实际／假设年度量及单位、期间和可复算推导 |
| 来源选择、组合与代表性 | `R.dataSelectionAndCombinationPrinciples` | 产品、技术、地区和年份的适配及限制有据 |
| 来源对象 | `R.referenceToDataSource` | 真实适用的 Source 与精确版本；不为假设虚构来源 |
| 原始数据采集期 | `R.dataCollectionPeriod` | 记录有据的采集期间，区别于假设年度基准 |
| 用途建议与限制 | `R.useAdviceForDataSet` | 原文在规范字段完整往返；平台生效状态见本次修复记录，原有方法字段内容保持 |
| 技术／地域专属适用性 | `P.processInformation.technology.technologicalApplicability`／`P.processInformation.geography.locationOfOperationSupplyOrProduction.descriptionOfRestrictions` | 仅承载该类限制，不重复整份声明 |

`P.processInformation.dataSetInformation.common:generalComment` 承载总体数据说明，方法字段承载推导和假设，专用 `useAdviceForDataSet` 承载用途建议与限制。作者按科学语义填写并核实际往返，不为绕过工具故障改变字段含义。

## 用途说明的保存路径

平台候选已将 `useAdviceForDataSet` 的保存路径改为与表单和读取相同的 `dataSourcesTreatmentAndRepresentativeness`。新候选直接使用规范字段；不再以搬到方法字段来规避序列化缺陷。已有原包迁移过的科学内容保留原件和映射，由原作者根据当前用途处理，不由工具自动搬回或删除。

2026-11-17，现役页面的 Data Check 已观察到隐式保存及遗漏 `useAdviceForDataSet`、分配、方法内容。其线上构建与已修源码的对应关系待核。RHA 个案原 `40900 t/year` 在 kg 参考单位界面产生了 `40900 kg`，恢复用等值 `40900000 kg/year`。核年度数字、单位与期间的等值关系，并核完整读回。上游部署后由维护者先验实际保存边界和无损往返，再交原负责人核自己的准确草稿。代码、证据、候选和不用该表单写入的工作可按原计划继续（历史过程回验留存 Git/本机维护日志）。[平台往返补证](https://github.com/tiangong-lca/platform/issues/432#issuecomment-6958247521) · [Data Check 动作边界](https://github.com/tiangong-lca/platform/issues/1528)

CLI 0.4.1 可接受空的 `common:referenceToCommissioner` 数组，现役页面对同一候选要求实际委托方。作者按原材料和已绑定规则填写真实 Contact，主审核所属用途的送审要求。公开规则的差异沿上游处理。[CLI #518](https://github.com/tiangong-lca/cli/issues/518)

## 原证据与计划接续

直接复用原作者包或 brief 中的字段映射／年度依据，不新增业务台账。结构化计划可在对应 `processes[]` 条目填写可选 `platform_field_evidence_ref: {path, sha256}`，引用同一原证据。未取证时为 `null` 并在原 `unknowns` 或 `checks` 写明下一步；已有计划可以不带此字段，继续原有效合同，不强迫重写。

原证据应让接手者找到：完整前像与候选、精确 Process／相关依赖、年度依据、实际 Platform/schema 来源、逐语言字段迁移、原生报告和保存读回，以及未验证的部分。记录实际源码映射、原生校验、独审或保存读回各自覆盖的结果。

## 验收与失败去向

- 使用所选原生入口构造完整候选，运行适用 SDK/CLI 四层检查，原包独审按完整差异检查年度数值含义与全部限制。
- 在原 owner 获准的保存／检查回验中，比较完整读回：年度数字、参考单位链与期间含义相符；所有原双语说明在所选稳定字段完整保留；交换数量、方向、精确 Flow 引用、分配、参考绑定及 Model 关系保持原目标要求。黏土反例中，20 项交换和年度／用途方法字段须分别验证。
- 接受有证据的时间戳、固定详情地址和引用说明变化，保留前后绑定并逐层读原生报告。
- 当前直接/Skill CLI 已删除 `annual_supply_or_production_volume_not_annualized` 的年度关键词推断。原生校验核字段格式，科学独审核年度量依据。平台修订已删除派生后缀、截断和 Process 隐式翻译；线上生效以实际部署与本机回验记录为准。旧平台的失败读回保留为证据，由维护处理部署，作者继续原包可独立推进的工作。

参考：[本次黏土保存反例](https://github.com/tiangong-lca/platform/issues/432#issuecomment-6524543748)、[固定年度后缀源码](https://github.com/tiangong-lca/platform/blob/80233595de60b0f6207e3306eef8ce566451d05f/src/services/processes/annualSupplyOrProductionVolume.ts#L385)、[用途映射源码](https://github.com/tiangong-lca/platform/blob/80233595de60b0f6207e3306eef8ce566451d05f/src/services/processes/util.ts#L461)。具体数据写入、工具修复、部署和发布沿原任务及明确范围执行。
