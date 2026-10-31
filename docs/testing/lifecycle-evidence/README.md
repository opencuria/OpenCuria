# Portable lifecycle audit evidence

This bundle accompanies [the audit report](../lifecycle-audit-report.md). The original 578 acceptance results remain unchanged. The [frozen original plan](plan-before-execution.md) is copied byte-for-byte; [case results](case-results.json) preserve every historical PASSED result.

JUnit files preserve exact testcase names, timings and pass/skip/error outcomes. Host metadata, captured stdout/stderr, properties and detailed skip text were removed. Fixture literals inside exact test names are intentional. VM/browser summaries preserve outcome assertions, timing and source digests while omitting resource identities, process metadata, inventory snapshots and request/auth state. The two screenshots show disposable local fixtures and a local full-desktop check. Source manifests identify historical checkpoints, not the later upstream merge.

Backend whole-suite baselines and the focused final projection delta are distinct; see [scope](backend/final-verification.json). U007 image disk minimum remains cross-layer: backend/real VM coverage, not a frontend minimum assertion. Earlier failed runs and raw commands/logs remain workspace-local; they are not passing evidence or published artifacts. No keys, journals, role credentials, database names or raw guest output are included.

[Later integration](integration/summary.json) is a separate verification after merging upstream, with its own JUnit and source manifests. It does not replace or expand the original acceptance snapshot.

[Provenance](provenance.json) records original artifact hashes and normalization. [Verification](verification-summary.json) checks preserved outcomes and links. [Bundle manifest](bundle-manifest.json) hashes portable files; it intentionally does not reuse the original absolute-link report hash.
