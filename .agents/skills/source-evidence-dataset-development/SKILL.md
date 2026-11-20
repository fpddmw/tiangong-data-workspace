---
name: source-evidence-dataset-development
description: Develop or revise evidence-supported TIDAS datasets from source documents, literature, reports, APIs and existing exact datasets; support the original author goal through complete scientific revision.
---

# Evidence-supported dataset development

The author owns the original package goal: determine scientific scope, gather evidence, create the candidate, check it, request independent scientific review and iterate. Use original task notes and sources directly; a stage return supplies the next decision and does not replace the package's actual completion condition.

Establish the functional unit, boundary, geography, period, intended use and known uncertainties from the brief and evidence. Gather the missing facts that affect the candidate, while advancing independent parts. Record source locations, original quantities/units, transformations and limitations in the original package's ordinary evidence notes. Search results locate sources; field claims need their underlying evidence. Where a critical fact cannot be established, explain the affected conclusion and a feasible next investigation.

## Research tools

Use the installed `tiangong-kb-sci-search` for scientific literature, `tiangong-kb-report-search` for reports, `academic-paper-download` for lawful OA retrieval and `document-granular-decompose` for supported document extraction. They are installed from `tiangong-ai/agent-skills`; current versions and availability are recorded in the workspace installation, not a new per-task registration document. Read the selected Skill and call it through `bin/env_run.py research` with absolute original-package output paths. Credentials remain in the selected service environment. A missing research service affects that channel; public sources and existing local material remain available.

## Author and revise

1. Fetch `dataset <type> contract`, use bounded search and exact get to decide reuse, and keep existing scientific identities across revisions.
2. Author canonical content from the evidence. Resolve the reference Flow/property/unit and amount before interpreting exchanges. Distinguish source facts, assumptions and authorized proxies.
3. Validate each current canonical document with `dataset <type> validate --input content.json --output report.json --json`. Inspect actual validation layers and completeness; use `list-references` and exact get for dependency evidence. Select online reference checks when needed.
4. For any numerical or boundary change, recompute affected quantities from the same candidate, reconcile meanAmount/resultingAmount/variables, and synchronize the full explanatory paragraphs, existing languages, tables and current report pointers. Review old statements remaining in those paragraphs. CLI validation currently does not prove quantitative or bilingual consistency.
5. Self-review the complete candidate against the evidence. Send scientifically consequential changes and unresolved questions to the independent reviewer, incorporate the feedback and continue the original goal. Reuse still-valid checks and opinions.
6. Authorized persistence uses distinct typed create/save and exact readback. The lead reviewer owns package adoption and authorized review submission; the author supplies the precise candidate and handles scientific revisions. Unknown writes continue through their original intent.

For an existing managed Foundry task, keep its supplied workspace, task, account and runtime, and return requested scientific input through its public actions. Its machine contract applies to that task; ordinary local authoring does not create a Foundry task or production queue. The work is complete when the requested scientific deliverable and any authorized handoff are actually met, with evidence, boundaries and remaining external dependencies clearly stated.
