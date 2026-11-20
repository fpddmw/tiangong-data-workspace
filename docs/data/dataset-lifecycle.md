# Dataset lifecycle and Model-derived Process contract

> 本页是迁入正式目录的后续维护版本；原文与已绑定历史回执保留原位。迁移不产生新的科学审核、运行成功或业务操作许可。
> [原始版本](../../work/control/dataset-lifecycle/CONTRACT.md)；来源 SHA-256：`97b7f16d23818609ea81662d2396f6baa51f226620f3233200a6bc7aa32426cf`。

Current governance revision: 2026-10-07. Before content revision, draft-version selection, review submission, or Model/resulting-Process work, load `python3 bin/context.py --role <role> --policy-key data_lifecycle` and verify its bound originals. This contract supplements P1–P5 and the [2026-10-01 state/group constraints](../../work/control/dataset-lifecycle/authorization-20261001.json). Normal production and historical public-data correction use the same workflow below. This revision defines their routing; it grants no permission to edit a published/review-state object, reject a review, withdraw, publish, delete or run a new remote calculation.

## State and fixed draft identity

| Observed state | Content action |
| --- | --- |
| `0`: draft | Revise only the applicable editor-owned, assigned exact draft; keep its UUID and version. |
| `20`: under review | Do not edit, reset its state or create a bypass successor. After an authorized review rejection, freshly verify that the original row is `0`, then revise that draft. |
| `100`: publicly published | Scientific changes normally use an applicable editable draft successor, then review, publication and verified replacement as required by the declared goal. No direct reset to `0`. Only the exact technical-correction exception below can permit a same-version content correction. |
| Other or unknown | Resolve the native lifecycle/ownership before mutation; visibility in the public 100–199 range does not make content editable. |

For a known object family, resolve the latest applicable editable owner-`0` version and its lineage before allocating another version. Reuse that exact draft through authoring, repair, retry and validation. If several drafts conflict, reconcile their intended lineage and writer rather than choosing arbitrarily or creating another one. A newer published baseline requires an explicit lineage/compatibility check; an old draft is not automatically the correct successor.

A new version is for an actual published-data revision when no suitable editable draft exists, not for every save, calculation, review finding or unknown-result retry. New identity still needs suitability/deduplication evidence. Preserve actual native version allocation and dependent-reference closure; do not predict a version or clone a review-state row to escape its gate.

Keep historical authorizations and receipts unchanged. A historical exception, a database credential or this governance discussion alone is not a current admission. Verify the exact operation's present authority, unconsumed scope and native capability; do not replay an old grant or infer permission for other objects.

## Historical correction within the same workflow

First classify the actual before/after change and its consequences, not the field name or the age of the row. A scientific change to identity, quantities, units, boundaries, assumptions or resulting exchanges uses the successor-draft route. Correct errors at their source and preserve the published preimage and lineage. Reuse an existing applicable state-0 successor before allocating a necessary new version.

A technical correction at the same published version is an exceptional route only when all of the following are evidenced in the existing repair package:

- A current, explicit authorization names the exact objects/versions, permitted fields or diff, operation, writer/account and intended post-state. The supported actual native or explicitly qualified database entry must match that authorization. General database access is not a substitute; an unsupported CLI cannot be replaced with ad hoc SQL or REST.
- Complete before and candidate bodies, exact differences, provenance, change meaning and relevant dependent impact are bound before execution. An independent reviewer assesses that diff in the existing complete Process package or standalone object review. Reuse valid unchanged science evidence; do not add a review or Root approval after every tool stage.
- Scientific meaning is unchanged, but operational effects are still assessed. Classification, reference repair, search/index behavior, provider selection, Model membership and downstream payload-hash bindings are not automatically harmless metadata. If meaning is changed or uncertain, use the scientific successor route or resolve that uncertainty; if an operational consequence is necessary, include its exact scope and checks.
- Fresh identity, owner/state and complete preimage still match at execution; preserve the original native attempt and recover UNKNOWN without replay. Independently read back the complete affected bodies, expected UUID/version/state (which may remain `100`), relations and necessary consumer consequences. Same-version correction changes payload hashes; retain both images and reassess affected derived/calculation evidence rather than rewriting historical receipts.

These are conditions on an exceptional operation, not a standing exception or a new approval chain. If exact authority or a qualified entry is absent, prepare the standard successor route or a bounded engineering Issue under the existing engineering contract. Keep the specific missing condition explicit; do not claim completion or create an arbitrary database bypass. State `20` remains under its original review and cannot use this exception.

Public withdrawal is a separate native lifecycle operation with its own exact authority, actual capability, original receipt and consumer-impact verification. Withdrawal does not imply state `0`, editability, deletion, reference closure or exclusion from Worker selection. Freshly observe its real state and references before selecting any subsequent action. Rejection of state `20` likewise belongs to the original review; only its verified return to state `0` permits editing that draft. This document does not establish that either platform operation is currently available.

## Model and resulting Process form a derived group

A Model describes selected unit-Process instances, their exact references, connections and coefficients. Select its reference Process instance and its reference exchange/Flow explicitly. Record the target quantity with its unit; the generated resulting Process must have that reference Flow and the target reference-exchange `meanAmount` after verified unit conversion. The Model calculation determines the required scaling of all unit Process instances.

Resulting-Process exchanges must come from the admitted Model generation result. Correct a contributing unit Process, Model connection/coefficient, reference selection or target quantity at its real source, then regenerate and verify affected results. Do not hand-patch derived exchanges, replace a calculated reference amount after generation, or retain a stale projection signature after changing its inputs. Independent metadata-only repairs must preserve derived exchanges and group bindings and pass the native path's actual checks.

Verify both payload references and database columns: a generated `process` row belongs to the exact Model through **`model_id` + `model_version`**. Neither a matching name nor JSON provenance alone proves this relation. Include all generated members required by the native operation; do not assume every Model always has exactly one resulting row. Check the exact Model and every affected result's owner/state/version before any grouped update. An editable Model does not authorize changing a published or review-state result.

## Execution and acceptance

Steps 1–2 apply when scientific inputs, exact references, connections, coefficients or the target quantity change, or when derived results need rebuilding. A metadata-only repair may reuse still-valid generation evidence: preserve exchanges and group bindings, then perform the applicable save/readback checks in steps 3–4. Do not demand a new calculation solely because a name or description changed.

1. Freeze the Model, all contributing Process versions, reference instance/exchange, target amount/unit, runtime and generation inputs. Use the platform-equivalent computation contract; an aggregator that consumes stored multipliers is not automatically a fresh solve.
2. Generate complete local native candidates and run the applicable structure, references, unit and scientific checks. Check reference Flow/amount, solved multipliers, internal-flow cancellation and preserved boundary exchanges. Do not invent missing identities or conversion factors.
3. Prepare the supported grouped save with exact draft identities, existing derived-row preimages and current writer/owner/state checks. Preserve the native result count and explicit successor behavior. Follow original-attempt recovery after UNKNOWN; never repair an uncertain group by resubmitting a new request.
4. Independently read back the Model and its resulting Process group. Match generated exchanges and reference amounts, `model_id/model_version`, UUID/version/state, intended membership and projection/dependency provenance. A returned Model row or an emitted relation file alone does not prove that the whole group was persisted correctly.
5. When revision is ready for review, enter the existing `0 → 20` checks/submission workflow and the review administrator's process. Do not edit state `20`; rejection and actual fresh state `0` reopen revision. Submission is distinct from administrator approval/publication.

Single-object draft acceptance, complete supply-network/LCI acceptance and publication remain separate. A local condition restricts only the objects/actions that depend on it. Do not turn global LCI closure into an unverified prerequisite for every valid draft save. When the declared goal is correction of publicly consumable data, a saved successor or review state `20` is only an intermediate result: verify the required publication/replacement and dependency/consumer consequences before closing that goal. A decision to withdraw, replace or delete is not its execution. Use the existing qualified deletion branch when deletion is actually selected; never force deletion to obtain a completion label.

Use the [scientific methods](../../workflows/dataset-review/README.md) appropriate to the original task. The author chooses the investigation and revision steps; the native operation owns authorization, state and readback. Historical operation identities and receipts remain intact.

[Model save capability evidence](../../work/control/dataset-lifecycle/model-save-capability-20261001.md) is loaded only when selecting a Model computation/save command. It describes an observed implementation, not permanent tool availability or authority.
