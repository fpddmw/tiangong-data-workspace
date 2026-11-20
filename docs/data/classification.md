# 数据分类选取与写前复核

> 本页是迁入正式目录的后续维护版本；原文与已绑定历史回执保留原位。迁移不产生新的科学审核、运行成功或业务操作许可。
> [原始版本](../../work/control/classification-authoring/PROCEDURE.md)；来源 SHA-256：`64ffa39785f9be035e1d2a6d6109a48d1a24cd22b971691dbc20f60784ac9904`。

适用：今后新建或修订天工数据时的分类选择；Process 行业分类按本页完整执行，其他类型先选其自己的 TIDAS 分类目录，不把 Process 的 ISIC 路径套到 Flow、Source 等对象。项目授权、单 writer、线上版本规则仍以当前 policy 与 `AGENTS.md` 为准。本页是数据作者的操作门禁，不宣称 CLI 已实现同等强制校验。

## 通过条件

1. **目录明确。** 记录数据类型、合格 CLI 所绑定的 TIDAS 分类资产、资产哈希与来源版本。新选 ISIC 行业分类以当前已核验的 ISIC Rev.5 为默认，并优先明确标作 `ISIC Rev.5`；例如现用 Process 目录的 `3511` 是 Rev.5 的“非可再生能源发电”，Rev.4 对应的发电类别是 `3510`。完整路径仍须与绑定资产及联合国统计司的当前结构逐级核对，不能只改 `@name` 来假装迁移。其他分类体系、旧版及缺少版本名的存量行要分别核查，不能一律重标；缺 `@name` 本身不等于错误。现用 Process 资产 SHA-256 为 `cea1b97b46fd9faa2f7f4d7b911ff19f24a1655ff98b488ebaf5cc9ee6fdec64`；[联合国 ISIC Rev.5 结构](https://unstats.un.org/unsd/classifications/Econ/isic)和[Rev.5 `3511` 说明](https://unstats.un.org/unsd/classifications/Econ/Structure/Detail/EN/2095/3511)是独立映射依据。
2. **活动相符。** Process 先写明模型边界、实际主活动、参考输出及辅助活动，再比较目录候选。参考产品是线索，不是把产品名机械映射成行业代码；含运输投入不自动归运输业，含煤投入不自动归采煤业。复合过程按有证据的主活动或共同类别选择；无法证明主活动时标为待审，不能用一个看似有效的根节点掩盖疑问。
3. **路径完整、深度有理。** 使用正式 CLI 的 `dataset classification children --type <type>` 逐级筛选，用 `dataset classification path --type <type> --code <selected>` 取得所选节点的完整祖先链。`children --query` 只筛当前父节点的直属子类；无命中不代表整个目录没有适合类别，要比较相邻分支及必要的权威类别说明。候选正文的代码、名称、次序须与同一目录的完整路径一致。默认选最具体且有证据支持的类别；不能下钻时记录具体原因和替代候选，交专家复核。不得无条件要求四级：真实混合活动可能只能落在有定义的中间节点。仅有一级且目录下有更具体子类的 Process，未经主活动与深度复核不得作为可送审分类。
4. **留存决定。** 每个新建或修改分类的 Process，在所属案例保存精确对象、原分类、所选代码和完整路径、目录绑定、主活动依据、被排除的相近类别、为何停在该层及复核状态。旧记录复制出的分类也要重新判断，不因 `path`、`apply` 或 `dataset validate` 返回成功就推定语义正确。Foundry 任务还须通过其公开 brief／interaction／semantic 入口绑定这一对象的分类决定，并核对实际采用回执；不手改其任务状态或把本页当成 Foundry 已自动采用的证明。
5. **写前和读回。** 直接 CLI 案例用 `dataset classification apply` 规范化本地候选，再运行该类型的正式原生校验；Foundry 案例沿其公开动作执行同等目录与语义复核。两者都须独立核对第 1–4 项。结构与语义均通过才进入已有 owner 写入门禁；仍有分类争议的对象保留草稿/待审，不发布。保存后 fresh get，核对精确版本的实际分类路径、目录声明和其余正文未被无关改写。公开已发布内容遵守冻结规则，本页不授予原位修改权。

## 当前工具边界

2026-09-26 用绑定本地 CLI 复核：`classification path` 接受中间节点；`apply` 只把所选代码正规化为路径；Process Schema 没有强制行业分类的最小深度，authoring/repair/publish 规则集中也没有行业分类语义检查。`dataset validate` 的成功只证明现有工具检查通过。分类写前检查由数据作者按上面的决定证据执行，正式门禁缺口跟踪在 [CLI Issue #292](https://github.com/tiangong-lca/cli/issues/292)。

本流程的案例复现与原生回执在 [分类核查案例](../../work/data-audit/classification-20260921/README.md)；2023 省级电力的已选中间节点和词表绑定在 [分类目录绑定](../../projects/electricity/runs/provincial-coverage/rebuild-20260926/2023-production/classification-catalog-binding.json)。
