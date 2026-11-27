# 质量分级合同（v1）

配套[数据质量分级设计](../data-quality-grading-design.md)（0.2，提案，未部署）的三份机器合同。它们是**提案的一部分**，不是已生效的政策、不是平台接口、不授予任何写入资格。

| 合同 | 谁写 | 作用 |
|---|---|---|
| [gate-claims.v1](schemas/gate-claims.v1.schema.json) | 阶段 1 编辑者 | 作者自检声明；**待核对的声明，不是证据** |
| [review-record.v1](schemas/review-record.v1.schema.json) | 阶段 2 审核员（AI 或人） | 独立复核结果：重算 diff、DQR、verdict、缺口 |
| [calculation-evidence.v1](schemas/calculation-evidence.v1.schema.json) | 阶段 3 计算方 | 绑定 model + demand 的可解性、覆盖、贡献 |

## 共用规则

1. 每份记录绑定 `(key, content_hash)`；正文或依赖变化即失效，不沿用旧记录。
2. `key` 形如 `processes:<uuid>@<version>`，类型取 `processes / flows / lifecyclemodels / sources`。
3. 未知写 `Not evaluated / unknown` 或 `null`，不填默认值。
4. 字段变更必须升版本（`…v2`），不原地改语义；旧实例保留。
5. 三份文件都是本地正本；平台字段是发布副本，AI 记录不得写入人类 review 字段。

## 样例

一条真实 state 20 对象的端到端样例（只读、未上传）：

[quality-stage1-20261112](../../../materials/packages/electricity-2023-lowvoltage-climate-tidas-20261029/publication-feasibility-20261030/goal582-20261101/quality-stage1-20261112/README.md)

## 校验

```bash
python3 - <<'PY'
import json
from jsonschema import Draft202012Validator
schema = json.load(open("docs/data/quality-grading/schemas/review-record.v1.schema.json"))
inst = json.load(open(".../review-record.v1.json"))
print(len(list(Draft202012Validator(schema).iter_errors(inst))), "errors")
PY
```
