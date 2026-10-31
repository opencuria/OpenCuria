# Historical backend verification scope

The complete 49-file baseline ran on SQLite (1501 passed, 21 skipped) and PostgreSQL (1521 passed, 1 skipped), 1522 test nodes each. A subsequent bounded four-file image-projection change was verified with focused final suites: SQLite 161 passed / 19 skipped and PostgreSQL 180 passed / 0 skipped, 180 nodes each. This is not a new complete suite on the final source.

The [scope and counts](final-verification.json), [baseline manifest](full-suite-source-sha256.json), [final 50-file manifest](final-code-sha256.json), and [134-case exact node mapping](coverage.json) preserve the checkpoint distinction. Database-specific skipped cases executed on the other database. [Architecture/lint/migration checks](checks.json) and [cleanup counts](cleanup.json) passed. Raw commands, role names and logs remain workspace-local.
