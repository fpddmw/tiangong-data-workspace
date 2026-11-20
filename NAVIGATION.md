# Agent retrieval and material custody

Start at AGENTS.md and the original task. Agents read source files directly; navigation gives locations and dated observations. Task owners decide scope and use. Native tools own their Sessions, attempts and write facts.

## Retrieval

- Follow an exact brief, candidate, source or receipt first. `context.py --task ID`, `--request ID`, `--query TERM` and `--scope ID` locate original records. Read only the methods relevant to the question.
- If an entry is missing, search the known package and provenance with `rg`; expand to relevant migration locations when evidence justifies it. `rg --no-ignore-vcs` can find unversioned scientific material. Keep credentials and native databases outside content searches.
- A missing index is an index failure, not evidence that its materials do not exist. The old HTAM asset catalog is unavailable on this Mac; current local task records, method files and readable package paths remain usable. Exact historical lookup and current limitations are in [.control/HISTORY](.control/HISTORY.md).
- A Git LFS pointer is not the source body. Use the project Git LFS entry and an exact selected path as described in [versioning](docs/workspace/versioning.md#本机准确取回-lfs-原件).
- History is available through explicit queries and original files. Dates, filenames, old next-action text and hashes do not establish current authority or online state. `harness.py history` reads former handoff records; current work uses [the task entry](workflows/business-flow/README.md).

## Placement

Use the original owner’s package or native location. A second consumer links the same original. Industry, account and publication state help select data; they do not create another material owner.

| Location | What belongs here |
|---|---|
| `docs`, `roles`, `workflows` | Current explanations, role responsibility and scientific methods |
| `materials/sources` | Source documents that need local custody |
| `materials/packages` | Author inputs, derivations, differences and review/engineering evidence not already held by a native tool |
| `projects` | A finite additional overall goal that the original task does not already express |
| `research`, `reports` | Reusable research or dated observations; existing package evidence stays in its package |
| `outputs`, `tmp` | User exports or unbound reproducible intermediates |
| `.control`, `bin`, `tests`, `installation` | Navigation, shared capabilities, verification and installation evidence |
| Existing `work` / native directories | Their original materials and tool-owned state |

Choose a new location only when needed, using the table above and the original task. Save and link the material directly; no storage registration or lifecycle record is required.

## Project and package entries

Keep one current reading entry for the original task: its effective goal, decisions, working material, evidence and unresolved responsibilities. An existing brief or native artifact index can serve this purpose. Replace obsolete status in the current entry; preserve the original history separately. Read the latest bytes before editing and retain concurrent changes.

A brief or receipt already bound to an exact hash remains an original. Its owner records a revised plan through the ordinary task action and points readers to it. Navigation edits do not transfer scientific writing responsibility or task ownership.

### Topic delivery views

[Topics](docs/topics/README.md) help readers locate existing goals and material. Add or update a view only when it improves a recurring lookup. Its links and dated limitations describe the known coverage; the original task supplies completion conditions. A shared-product link does not require a new system model or downstream adoption exercise.

Use the [topic example](docs/topics/topic-template.md) if useful. Update affected links when the owner’s current entry changes, keeping actual progress at that original entry. System integration evidence is relevant when the original goal includes system integration; the [system method](roles/SYSTEM-PLANNING.md) explains that work.

## Historical pointers

Existing storage indexes, object keys and context packets remain historical lookup records. They confer no current permission or executable next step. Current work uses the task’s original files and public harness commands; an index is not a required production form.

## Finish, extend or relate an operation

Finish or explicitly stop the original goal in place. A new year or consumer adds a necessary relation, not a forced move. A genuinely new purpose may need its own original task; shared sources remain shared. Original business ownership and actual authorization determine what continues.

## Relocation

Keep bound originals and path-dependent execution material in place. Where a real move is required, use the owning tool’s supported procedure and verify the current consumers and exact bytes. Native Session/attempt IDs, outcomes and unknown effects remain with that tool. Installed skills, runtimes, credentials and active databases are maintained through their own installation/recovery interfaces.

## Retention and cleanup events

Retain accepted scientific evidence, source assumptions, complete readback and recovery history. Completion alone does not make them disposable. Remove an intermediate only when its exact owner, lack of binding and reproducibility are established. Preserve stopped, waiting and UNKNOWN attempts until their original effects are accounted for.

## Navigation and versioning

The private GitLab repository stores authorized source, guidance and selected scientific evidence. Credentials, native databases/sidecars, live locks, installations and machine sessions stay outside Git. Review actual staged bytes and run the applicable [versioning checks](docs/workspace/versioning.md). History and existing materials remain where they are; code cleanup removes obsolete execution paths without deleting their evidence.
