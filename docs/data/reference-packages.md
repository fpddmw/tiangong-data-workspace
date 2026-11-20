# Reference-package identity and protection

This workspace preserves the identity and scientific content of source-bound reference packages while developing TianGong data around them. Current membership and any temporary exclusions are selected by `policy.reference_data_protection`; the package's original files and evidence remain with their original owner. A reference account is a custody choice, not a blanket exemption or permanent freeze of everything in that account.

## Identify before changing

Within each TianGong dataset table, the database object key is **UUID + version**. Cross-type evidence, manifests, requests and comparisons use **dataset type + UUID + version**. A UUID by itself identifies a family, not one object; a family count is not the count of exact versions or qualified functional demands. Preserve exact version strings and source versions, and never resolve a requested version by silently taking the latest.

Keep the scientific UUID in the document distinct from an explicitly registered database storage locator. An existing verified alias is a lookup relationship: bind both identities and their version in evidence, keep consumer references scientific, and do not rewrite primary keys or infer aliases by name. A known alias in one dataset type does not apply to another.

## Apply the active protection scope

Before proposing a write set, load the selected manifest and verify its hashes. Match the mutation's dataset type and UUID against the protected family set, including explicitly listed storage aliases and source-bound unresolved dependencies. Preserve exact UUID/version membership for provenance and replay checks; use the manifest's declared all-version rule for the mutation exclusion. Creating another version of the same UUID does not evade that rule.

For an active “no modification” scope, the Agent does not initiate changes to the protected object: scientific or administrative content, ownership, state, deletion, derivative rebuilding, or insertion of another version in the protected family. Read-only use and references from consumers remain available, subject to actual visibility and scientific applicability. Repair a consumer's wrong reference, quantity, unit, direction or compartment using qualified existing dependencies rather than altering the reference package to fit it.

An object referenced by the package but absent from its files is explicitly marked as a reference-only dependency, with unknown source version when unspecified. A protective hold on that UUID does not prove its body exists, its source is complete or its science is accepted. Different bodies sharing an exact source key are retained as a discrepancy; the manifest identifies the authoritative package member and any converter/resource copies without silently merging them.

If a protected object really needs a change, retain the exact discrepancy, affected versions and consumers. A later explicit user decision can revise that precise protection scope; an ordinary draft permission, general cleanup request, account transfer or new upstream package does not implicitly release it. Keep prior source bytes, manifests and decisions as historical evidence.

## Verification and current binding

Record the candidate write-set intersection and allowed read-only dependencies separately. Verify the complete preimage, current identity/state, original writer and native checks for an admitted operation; then read back its actual outcome. A zero intersection is only a scope check, not scientific or write acceptance.

The active package hashes and original authorization are discoverable with `python3 bin/context.py --role coordinator --policy-key reference_data_protection`. The 2026-10-13 EF 3.1 freeze is bound there to the user's supplied `EF-3.1-2-2026` source package and its generated exclusion files; this contract does not permanently select one EF release for every future project.

This is a workspace execution contract. It does not claim database-enforced immutability, stop external writers, or add SQL triggers/RLS. Tool/server enforcement must be stated from actual evidence. Named exceptional operations remain in their original authorization and attempt records; they do not become a general permission to bypass native controls.
