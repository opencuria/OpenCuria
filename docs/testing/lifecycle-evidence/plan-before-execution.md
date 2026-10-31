# Frozen lifecycle verification plan

Recorded 2026-10-31T06:36:55.916234+00:00 before any new test execution. Expected outcomes remain immutable in this snapshot.


---

# Backend lifecycle test cases

Plan recorded before test execution. Every row is initially NOT RUN. PASS requires the named assertion to execute successfully against the final code; unit/database simulation is explicitly distinguished from real VM tests in the consolidated report.

| ID | Scenario and expected outcome | Verification | Status |
|---|---|---|---|
| B001 | create_workspace, sealed interrupted journal, runtime running: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace-running]` | NOT RUN |
| B002 | create_workspace, sealed interrupted journal, runtime stopped: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace-stopped]` | NOT RUN |
| B003 | create_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace-absent]` | NOT RUN |
| B004 | create_workspace_from_image_artifact, sealed interrupted journal, runtime running: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace_from_image_artifact-running]` | NOT RUN |
| B005 | create_workspace_from_image_artifact, sealed interrupted journal, runtime stopped: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace_from_image_artifact-stopped]` | NOT RUN |
| B006 | create_workspace_from_image_artifact, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace_from_image_artifact-absent]` | NOT RUN |
| B007 | stop_workspace, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[stop_workspace-running]` | NOT RUN |
| B008 | stop_workspace, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[stop_workspace-stopped]` | NOT RUN |
| B009 | stop_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[stop_workspace-absent]` | NOT RUN |
| B010 | resume_workspace, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[resume_workspace-running]` | NOT RUN |
| B011 | resume_workspace, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[resume_workspace-stopped]` | NOT RUN |
| B012 | resume_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[resume_workspace-absent]` | NOT RUN |
| B013 | update_workspace, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[update_workspace-running]` | NOT RUN |
| B014 | update_workspace, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[update_workspace-stopped]` | NOT RUN |
| B015 | update_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[update_workspace-absent]` | NOT RUN |
| B016 | remove_workspace, sealed interrupted journal, runtime running: release only this fence; delete_failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[remove_workspace-running]` | NOT RUN |
| B017 | remove_workspace, sealed interrupted journal, runtime stopped: release only this fence; delete_failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[remove_workspace-stopped]` | NOT RUN |
| B018 | remove_workspace, sealed interrupted journal, runtime absent: release only this fence; delete_failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[remove_workspace-absent]` | NOT RUN |
| B019 | build_image, sealed interrupted journal, runtime running: release only this fence; failed generation / prior current preserved; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[build_image-running]` | NOT RUN |
| B020 | build_image, sealed interrupted journal, runtime stopped: release only this fence; failed generation / prior current preserved; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[build_image-stopped]` | NOT RUN |
| B021 | build_image, sealed interrupted journal, runtime absent: release only this fence; failed generation / prior current preserved; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[build_image-absent]` | NOT RUN |
| B022 | create_image_artifact, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_image_artifact-running]` | NOT RUN |
| B023 | create_image_artifact, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_image_artifact-stopped]` | NOT RUN |
| B024 | create_image_artifact, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_image_artifact-absent]` | NOT RUN |
| B025 | delete_image, sealed interrupted journal, runtime running: release only this fence; delete_failed / retry available; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[delete_image-running]` | NOT RUN |
| B026 | delete_image, sealed interrupted journal, runtime stopped: release only this fence; delete_failed / retry available; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[delete_image-stopped]` | NOT RUN |
| B027 | delete_image, sealed interrupted journal, runtime absent: release only this fence; delete_failed / retry available; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[delete_image-absent]` | NOT RUN |
| B028 | Unsafe evidence runner: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[runner]` | NOT RUN |
| B029 | Unsafe evidence operation_id: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[operation_id]` | NOT RUN |
| B030 | Unsafe evidence attempt: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[attempt]` | NOT RUN |
| B031 | Unsafe evidence target: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[target]` | NOT RUN |
| B032 | Unsafe evidence instance: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[instance]` | NOT RUN |
| B033 | Unsafe evidence finished: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[finished]` | NOT RUN |
| B034 | Unsafe evidence quiescent: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[quiescent]` | NOT RUN |
| B035 | Unsafe evidence running: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[running]` | NOT RUN |
| B036 | Unsafe evidence unsealed: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[unsealed]` | NOT RUN |
| B037 | Unsafe evidence known_unfinished: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[known_unfinished]` | NOT RUN |
| B038 | Unsafe evidence missing_outcome: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[missing_outcome]` | NOT RUN |
| B039 | Unsafe evidence replaced_session: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[replaced_session]` | NOT RUN |
| B040 | create_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[create_workspace]` | NOT RUN |
| B041 | create_workspace_from_image_artifact, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[create_workspace_from_image_artifact]` | NOT RUN |
| B042 | stop_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[stop_workspace]` | NOT RUN |
| B043 | resume_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[resume_workspace]` | NOT RUN |
| B044 | update_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[update_workspace]` | NOT RUN |
| B045 | remove_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[remove_workspace]` | NOT RUN |
| B046 | build_image, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[build_image]` | NOT RUN |
| B047 | create_image_artifact, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[create_image_artifact]` | NOT RUN |
| B048 | delete_image, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[delete_image]` | NOT RUN |
| B049 | create_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[create_workspace]` | NOT RUN |
| B050 | create_workspace_from_image_artifact, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[create_workspace_from_image_artifact]` | NOT RUN |
| B051 | stop_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[stop_workspace]` | NOT RUN |
| B052 | resume_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[resume_workspace]` | NOT RUN |
| B053 | update_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[update_workspace]` | NOT RUN |
| B054 | remove_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[remove_workspace]` | NOT RUN |
| B055 | build_image, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[build_image]` | NOT RUN |
| B056 | create_image_artifact, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[create_image_artifact]` | NOT RUN |
| B057 | delete_image, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[delete_image]` | NOT RUN |
| B058 | create_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[create_workspace]` | NOT RUN |
| B059 | create_workspace_from_image_artifact, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[create_workspace_from_image_artifact]` | NOT RUN |
| B060 | stop_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[stop_workspace]` | NOT RUN |
| B061 | resume_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[resume_workspace]` | NOT RUN |
| B062 | update_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[update_workspace]` | NOT RUN |
| B063 | remove_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[remove_workspace]` | NOT RUN |
| B064 | build_image, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[build_image]` | NOT RUN |
| B065 | create_image_artifact, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[create_image_artifact]` | NOT RUN |
| B066 | delete_image, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[delete_image]` | NOT RUN |
| B067 | create_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[create_workspace]` | NOT RUN |
| B068 | create_workspace_from_image_artifact, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[create_workspace_from_image_artifact]` | NOT RUN |
| B069 | stop_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[stop_workspace]` | NOT RUN |
| B070 | resume_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[resume_workspace]` | NOT RUN |
| B071 | update_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[update_workspace]` | NOT RUN |
| B072 | remove_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[remove_workspace]` | NOT RUN |
| B073 | build_image, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[build_image]` | NOT RUN |
| B074 | create_image_artifact, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[create_image_artifact]` | NOT RUN |
| B075 | delete_image, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[delete_image]` | NOT RUN |
| B076 | stop_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[stop_workspace]` | NOT RUN |
| B077 | resume_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[resume_workspace]` | NOT RUN |
| B078 | update_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[update_workspace]` | NOT RUN |
| B079 | remove_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[remove_workspace]` | NOT RUN |
| B080 | create_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[create_workspace]` | NOT RUN |
| B081 | create_image_artifact, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[create_image_artifact]` | NOT RUN |
| B082 | capture-stop, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[capture-stop]` | NOT RUN |
| B083 | capture-image, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[capture-image]` | NOT RUN |
| B084 | recreate-remove, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[recreate-remove]` | NOT RUN |
| B085 | recreate-create, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[recreate-create]` | NOT RUN |
| B086 | Credential initial=False, proof=None: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[False-None]` | NOT RUN |
| B087 | Credential initial=False, proof=False: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[False-False]` | NOT RUN |
| B088 | Credential initial=False, proof=True: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[False-True]` | NOT RUN |
| B089 | Credential initial=True, proof=None: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[True-None]` | NOT RUN |
| B090 | Credential initial=True, proof=False: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[True-False]` | NOT RUN |
| B091 | Credential initial=True, proof=True: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[True-True]` | NOT RUN |
| B092 | Interrupted build while prior current image exists: failed pending generation never replaces current image; retry allocates new generation. | `test_interrupted_builds.py::test_interruption_and_explicit_close_reconcile_build_assignment` | NOT RUN |
| B093 | Capture closure keeps ready/deleting images and marks unfinished reservation failed. | `test_capture_disposition.py` | NOT RUN |
| B094 | Unknown outcome is not represented as execution success; diagnostic explains interruption and safe retry, not admin work. | `test_automatic_recovery_matrix.py::test_user_diagnostic` | NOT RUN |
| B095 | Never-delivered offline prepared operation retains original identity beyond deadline and continues after reconnect. | `test_automatic_recovery_matrix.py::test_never_delivered_offline_stays_queued` | NOT RUN |
| B096 | Unreproducible credential payload before first delivery fails locally and releases workspace fence without disclosing secrets. | `test_automatic_recovery_matrix.py::test_unreproducible_input_fails_without_fence` | NOT RUN |
| B097 | Full backend runner suite SQLite: all relevant lifecycle, auth, inventory, recipes, settings and migrations remain green. | `pytest apps/runners/tests (SQLite)` | NOT RUN |
| B098 | Full backend runner suite PostgreSQL: real row locks, CAS races, simultaneous allocation and historical migrations remain green. | `pytest apps/runners/tests (PostgreSQL)` | NOT RUN |

---

# Runner lifecycle verification cases

Plan recorded before new test execution. Every parameter combination of a mapped test must pass before its row becomes PASS. Evidence comes from the final pytest JUnit report; fault-injection tests do not claim physical SSD or power-controller failure reproduction.

| ID | Result | Scenario and expected result | Exact automated test mapping |
| --- | --- | --- | --- |
| R001 | NOT RUN | Restart after admitted create: retain failure tombstone; never replay init or persist credentials. | `runner/tests/test_operation_journal.py::test_reopen_interrupted_never_reexecutes` |
| R002 | NOT RUN | Wrong-attempt ACK retains result; matching ACK stops replay; restart still prevents duplicate execution. | `runner/tests/test_operation_journal.py::test_terminal_replay_ack_retains_tombstone` |
| R003 | NOT RUN | Duplicate delivery, cancelled caller and disconnected backend execute once; terminal result replays after reconnection. | `runner/tests/test_operation_journal.py::test_duplicate_and_outage_dont_abort_work` |
| R004 | NOT RUN | Second journal owner rejected; arbitrary runtime error secrets never persisted in terminal result. | `runner/tests/test_operation_journal.py::test_journal_exclusive_process_and_terminal_sanitization` |
| R005 | NOT RUN | Restart retains checkpoint only for identical runtime incarnation. | `runner/tests/test_operation_journal.py::test_checkpoint_survives_restart_only_exact_incarnation` |
| R006 | NOT RUN | Inspection distinguishes active operation from durable interrupted result without claiming completion early. | `runner/tests/test_operation_journal.py::test_inspect_running_and_interrupted_are_distinct` |
| R007 | NOT RUN | Never-delivered operation is inspected without starting its recipe; only complete quiet inventory proves quiescence. | `runner/tests/test_operation_journal.py::test_unrecorded_identity_can_inspect_fresh_quiescence_without_execution` |
| R008 | NOT RUN | Transient resume error retries at most three times; terminal result appears only after execution ends. | `runner/tests/test_operation_journal.py::test_safe_transient_start_retry_is_bounded_and_terminal_only_after_finish` |
| R009 | NOT RUN | Capture ENOSPC after exact scrub checkpoint is a known failure with safe source retained. | `runner/tests/test_operation_journal.py::test_finished_capture_enospc_with_exact_scrub_checkpoint_stays_known` |
| R010 | NOT RUN | Sealed operation rejects delayed execution permanently. | `runner/tests/test_operation_journal.py::test_sealed_unrecorded_identity_cannot_execute_delayed_recipe` |
| R011 | NOT RUN | Exception after possible effect records ambiguous outcome rather than inventing success. | `runner/tests/test_operation_journal.py::test_effect_then_generic_exception_is_unknown` |
| R012 | NOT RUN | Running readiness tolerates disk writes; stopped scrub evidence requires unchanged disk identity/stat. | `runner/tests/test_operation_journal.py::test_running_readiness_allows_normal_writes_but_scrub_requires_exact_stat` |
| R013 | NOT RUN | Unexpected guest boot permanently revokes stopped scrub proof. | `runner/tests/test_operation_journal.py::test_observed_unexpected_boot_permanently_revokes_scrub` |
| R014 | NOT RUN | Resume checkpoint and published terminal identity match. | `runner/tests/test_operation_journal.py::test_resume_result_checkpoint_matches_terminal_envelope` |
| R015 | NOT RUN | Repeated cancellation during delayed fsync keeps IO ownership until actual commit completes. | `runner/tests/test_journal_io_boundaries.py::test_repeated_cancellation_drains_sqlite_before_releasing_io_queue` |
| R016 | NOT RUN | Cancellation during admission fsync neither loses execution nor duplicates it; shutdown joins admitted work. | `runner/tests/test_journal_io_boundaries.py::test_cancelled_delivery_during_fsync_still_executes_once_and_drains` |
| R017 | NOT RUN | Delayed final inspection excludes conflicting same-target work while independent workspace executes. | `runner/tests/test_journal_io_boundaries.py::test_terminal_inspection_keeps_target_fenced_but_allows_other_targets` |
| R018 | NOT RUN | One failed runtime inspection does not starve other recovery records; later tick retries it. | `runner/tests/test_journal_io_boundaries.py::test_recovery_retries_transient_inspection_and_continues_other_records` |
| R019 | NOT RUN | Live orphan disk helper retains unfinished operation and target fence until helper quiesces. | `runner/tests/test_journal_io_boundaries.py::test_orphan_helpers_keep_crashed_operations_unfinished_and_target_fenced` |
| R020 | NOT RUN | Late terminal delivery cannot replace a newer lifecycle checkpoint. | `runner/tests/test_journal_io_boundaries.py::test_terminal_delivery_does_not_overwrite_lifecycle_checkpoint` |
| R021 | NOT RUN | Actual Linux live helper blocks; actual unreaped zombie is quiescent. | `runner/tests/test_journal_io_boundaries.py::test_exited_unreaped_helper_is_quiescent_but_live_helper_is_not` |
| R022 | NOT RUN | Capture recovery requires exact operation/image/source tuple; wrong source rejected. | `runner/tests/test_journal_io_boundaries.py::test_capture_recovery_matches_workspace_target_and_published_image_identity` |
| R023 | NOT RUN | Unverified orphan build guest retains fence; complete stopped observation permits interrupted result. | `runner/tests/test_journal_io_boundaries.py::test_orphan_guest_script_keeps_build_unfinished_until_guest_stops` |
| R024 | NOT RUN | Real aiohttp/Socket.IO: unopened backend, duplicate delivery, backend shutdown during create, offline completion, reconnect and exact ACK yield one effect. | `runner/tests/test_lifecycle_transport_integration.py::test_wire_outage_keeps_lifecycle_and_replays_durable_terminal_once` |
| R025 | NOT RUN | Started guest with failed credential injection remains ambiguous; no false ready status. | `runner/tests/test_capture_resume_failure.py::test_started_guest_with_failed_injection_is_unknown` |
| R026 | NOT RUN | No-effect failure may report known credentials only with matching live checkpoint. | `runner/tests/test_capture_resume_failure.py::test_known_no_effect_failure_uses_exact_checkpoint` |
| R027 | NOT RUN | Missing/replaced incarnation cannot inherit cached credential proof. | `runner/tests/test_capture_resume_failure.py::test_missing_or_changed_runtime_proof_cannot_use_cache` |
| R028 | NOT RUN | Runtime inspection failure cannot claim known outcome. | `runner/tests/test_capture_resume_failure.py::test_inspection_failure_is_unknown` |
| R029 | NOT RUN | Recovery preserves explicit ambiguity even when execution has ended. | `runner/tests/test_capture_resume_failure.py::test_journal_recovery_preserves_explicit_unknown_outcome` |
| R030 | NOT RUN | Partial stop without exact scrub proof publishes unknown outcome. | `runner/tests/test_capture_resume_failure.py::test_partial_stop_without_proof_emits_unknown_wrapped_result` |
| R031 | NOT RUN | Known stop failure uses fresh live checkpoint, not stale cache. | `runner/tests/test_capture_resume_failure.py::test_known_stop_failure_uses_live_checkpoint` |
| R032 | NOT RUN | Inventory during create/clone/resume cannot detach registry object; immediate stop uses committed identity. | `runner/tests/test_registry_lifecycle_race.py::test_sync_during_real_handler_and_immediate_next_stop` |
| R033 | NOT RUN | Snapshot begun before lifecycle cannot overwrite newer completion. | `runner/tests/test_registry_lifecycle_race.py::test_snapshot_started_before_operation_cannot_overwrite_completion` |
| R034 | NOT RUN | Merge retains credential presence only for same physical instance. | `runner/tests/test_registry_lifecycle_race.py::test_same_instance_merge_preserves_credentials_different_instance_does_not` |
| R035 | NOT RUN | Stop uses actual running status despite stale stopped cache. | `runner/tests/test_registry_lifecycle_race.py::test_live_running_status_overrides_stale_stopped_cache` |
| R036 | NOT RUN | Failed status probe causes no credential mutation or VM stop. | `runner/tests/test_registry_lifecycle_race.py::test_failed_live_observation_never_scrubs_or_stops` |
| R037 | NOT RUN | Clone initialization retains creating registry entry through inventory. | `runner/tests/test_registry_lifecycle_race.py::test_clone_preserves_creating_entry_and_identity_during_inventory` |
| R038 | NOT RUN | Failed inventory/resume cannot restore stale credential certainty. | `runner/tests/test_registry_lifecycle_race.py::test_failed_refresh_and_failed_resume_keep_unknown_credentials` |
| R039 | NOT RUN | Partially allocated failed create remains visible for reconciliation. | `runner/tests/test_registry_lifecycle_race.py::test_incomplete_create_failure_is_preserved_for_recovery` |
| R040 | NOT RUN | Duplicate base/clone create cannot erase existing physical identity. | `runner/tests/test_registry_lifecycle_race.py::test_duplicate_create_preserves_existing_physical_registry_identity` |
| R041 | NOT RUN | Credential replacement updates presence and revokes older scrub proof before write. | `runner/tests/test_registry_lifecycle_race.py::test_credential_replacement_updates_presence_and_invalidates_old_proof` |
| R042 | NOT RUN | Partial credential scrub cannot leave old initialized proof trusted. | `runner/tests/test_registry_lifecycle_race.py::test_partial_scrub_revokes_running_proof_before_any_credential_write` |
| R043 | NOT RUN | Idempotent remove without cache succeeds only after complete exact physical absence proof. | `runner/tests/test_registry_lifecycle_race.py::test_missing_workspace_removal_requires_complete_physical_absence` |
| R044 | NOT RUN | Delayed running inventory cannot revoke newly committed stopped scrub checkpoint. | `runner/tests/test_registry_lifecycle_race.py::test_stale_running_observation_cannot_revoke_new_stop_checkpoint` |
| R045 | NOT RUN | Workspace collection mutation during observation does not crash observation loop. | `runner/tests/test_websocket_interface.py::test_observation_tolerates_workspace_changes_during_probe` |
| R046 | NOT RUN | Delete forwards concrete managed image reference. | `runner/tests/test_websocket_interface.py::test_image_deletion_uses_runtime_reference` |
| R047 | NOT RUN | Initial backend outage is retried with bounded delay instead of process exit. | `runner/tests/test_websocket_interface.py::test_start_retries_initial_backend_outage_without_leaving_runner` |
| R048 | NOT RUN | Runner shutdown interrupts startup reconnect backoff promptly. | `runner/tests/test_websocket_interface.py::test_stop_wakes_startup_backoff_without_waiting_for_backend` |
| R049 | NOT RUN | Unsupported legacy lifecycle task handlers are absent. | `runner/tests/test_websocket_interface.py::test_retired_task_handlers_are_not_registered` |
| R050 | NOT RUN | Handshake timeout is normalized to retryable transport failure. | `runner/tests/test_websocket_interface.py::test_reconnect_loop_survives_handshake_timeouts` |
| R051 | NOT RUN | Configured transport accepts supported large file payload size. | `runner/tests/test_websocket_interface.py::test_socketio_client_raises_http_buffer_size` |
| R052 | NOT RUN | Maintenance-observed desktop exit is reported consistently. | `runner/tests/test_websocket_interface.py::test_observation_announces_desktop_ended_by_maintenance` |
| R053 | NOT RUN | Initial connection inventory is reused rather than immediately repeating expensive scan. | `runner/tests/test_websocket_interface.py::test_connect_snapshot_is_reused_for_first_heartbeat` |
| R054 | NOT RUN | Desktop recovery precedes state reannouncement on reconnect. | `runner/tests/test_websocket_interface.py::test_connect_recovers_desktop_sessions_before_reannounce` |
| R055 | NOT RUN | Blocked disk inventory cannot prevent health heartbeat or active operation lease renewal. | `runner/tests/test_websocket_interface.py::test_blocked_inventory_does_not_block_health_or_operation_lease` |
| R056 | NOT RUN | Capture drains admitted interactions and rejects new writes before scrub proof. | `runner/tests/test_capture_fence.py::test_capture_drains_admitted_work_before_proof_and_rejects_new_work` |
| R057 | NOT RUN | Capture failure/cancellation releases its owned interaction fence. | `runner/tests/test_capture_fence.py::test_capture_cleanup_on_runtime_failure_or_cancellation` |
| R058 | NOT RUN | Cancellation while draining does not strand capture fence. | `runner/tests/test_capture_fence.py::test_cancel_capture_while_draining_releases_fence` |
| R059 | NOT RUN | Caller clean flag cannot bypass exact credential scrub proof. | `runner/tests/test_capture_fence.py::test_client_clean_flag_does_not_replace_scrub_proof` |
| R060 | NOT RUN | All manager mutation surfaces participate in capture fence. | `runner/tests/test_capture_fence.py::test_composed_manager_interactions_are_guarded` |
| R061 | NOT RUN | Session-only write resolves original workspace before capture fence. | `runner/tests/test_capture_fence.py::test_session_id_only_writes_use_original_workspace` |
| R062 | NOT RUN | Capture-rejected exec preserves correlated error envelope. | `runner/tests/test_capture_fence.py::test_websocket_exec_rejection_keeps_result_envelope` |
| R063 | NOT RUN | Desktop proxy mutation respects capture fence. | `runner/tests/test_capture_fence.py::test_desktop_proxy_paths_are_guarded` |
| R064 | NOT RUN | Concurrent second capture is rejected without releasing first owner. | `runner/tests/test_capture_fence.py::test_second_capture_is_rejected_and_does_not_release_first` |
| R065 | NOT RUN | Live write queued before stop is rejected after lifecycle epoch changes. | `runner/tests/test_capture_fence.py::test_live_call_queued_behind_stop_is_rejected_after_epoch_change` |
| R066 | NOT RUN | Previously queued lifecycle cannot mutate inside newer capture boundary. | `runner/tests/test_capture_fence.py::test_lifecycle_queued_before_capture_cannot_run_inside_capture` |
| R067 | NOT RUN | Stop closes active stream read before capture waits for drain. | `runner/tests/test_capture_fence.py::test_stop_closes_active_stream_read_before_capture_drains` |
| R068 | NOT RUN | Desktop waiter revalidates epoch after stop. | `runner/tests/test_capture_fence.py::test_admitted_desktop_waiter_is_rechecked_after_stop_epoch` |
| R069 | NOT RUN | Credential archive conversion rejects symlink and hardlink entries. | `runner/tests/test_workspace_service_security.py::test_convert_archive_to_tar_rejects_links` |
| R070 | NOT RUN | Create injects credentials before repository clone and reports resulting presence. | `runner/tests/test_workspace_service_security.py::test_create_workspace_clones_with_persistent_credentials` |
| R071 | NOT RUN | Credential files are materialized through the canonical injector. | `runner/tests/test_workspace_service_security.py::test_inject_workspace_credentials_materializes_file_credentials` |
| R072 | NOT RUN | Empty credential set produces explicit absent presence. | `runner/tests/test_workspace_service_security.py::test_inject_workspace_credentials_empty_set_returns_false` |
| R073 | NOT RUN | Controlled stop scrubs credentials before stopping runtime. | `runner/tests/test_workspace_service_security.py::test_stop_workspace_removes_credentials_before_runtime_stop` |
| R074 | NOT RUN | Terminal uses canonical persistent workspace environment. | `runner/tests/test_workspace_service_security.py::test_start_terminal_sources_persistent_env` |
| R075 | NOT RUN | Missing delivery with complete quiet observation is sealed automatically before inspection publication. | `runner/tests/test_automatic_operation_recovery.py::test_inspect_request_seals_quiescent_missing_identity` |
| R076 | NOT RUN | Incomplete/live-helper/live-build inventory cannot auto-seal missing identity. | `runner/tests/test_automatic_operation_recovery.py::test_inspect_request_does_not_seal_unverified_missing_identity` |
| R077 | NOT RUN | Concurrent admission invalidates prior quiescence; seal cannot race fresh execution. | `runner/tests/test_automatic_operation_recovery.py::test_inspect_request_seal_rechecks_admission_epoch` |
| R078 | NOT RUN | Inspection reports running/stopped/absent/unknown from complete exact inventory. | `runner/tests/test_automatic_operation_recovery.py::test_inspection_includes_exact_workspace_state` |
| R079 | NOT RUN | Credential presence is included only for exact incarnation proof with matching observed state. | `runner/tests/test_automatic_operation_recovery.py::test_inspection_credential_presence_requires_exact_proof` |
| R080 | NOT RUN | Other workspace or incomplete inventory cannot establish target state. | `runner/tests/test_automatic_operation_recovery.py::test_inspection_never_uses_wrong_workspace_inventory` |
| R081 | NOT RUN | Interrupted build cleanup targets only operation-owned temporary instance; no recipe replay. | `runner/tests/test_automatic_operation_recovery.py::test_recovery_cleans_only_exact_interrupted_build` |
| R082 | NOT RUN | Failed orphan cleanup keeps unfinished fence and retries next recovery tick. | `runner/tests/test_automatic_operation_recovery.py::test_recovery_retries_failed_build_cleanup` |
| R083 | NOT RUN | Published image is recovered as success after orphan cleanup, not overwritten by failure. | `runner/tests/test_automatic_operation_recovery.py::test_recovery_successful_publication_wins_after_build_cleanup` |
| R084 | NOT RUN | Concurrent completed lifecycle epoch invalidates quiescence even if no operation currently running. | `runner/tests/test_automatic_operation_recovery.py::test_late_old_observation_does_not_claim_quiescence` |
| R085 | NOT RUN | Automatically sealed missing identity has interrupted diagnostic and no manual-intervention instruction. | `runner/tests/test_automatic_operation_recovery.py::test_sealed_missing_identity_uses_retryable_diagnostic` |
| R086 | NOT RUN | Recovery terminal fsync retains target fence until actual durable commit. | `runner/tests/test_automatic_operation_recovery.py::test_unfinished_recovery_keeps_target_bound_until_commit` |
| R087 | NOT RUN | Recovery never cleans a currently running executor build. | `runner/tests/test_automatic_operation_recovery.py::test_live_same_process_build_is_not_cleaned_by_recovery` |
| R088 | NOT RUN | Cleanup return alone is insufficient; runtime must prove temporary guest and resources absent. | `runner/tests/test_automatic_operation_recovery.py::test_recovery_waits_for_exact_runtime_absence` |
| R089 | NOT RUN | Exact persisted stopped scrub checkpoint restores completed stop after restart with no handler replay. | `runner/tests/test_automatic_operation_recovery.py::test_restart_recovers_stop_checkpoint_without_rerun` |
| R090 | NOT RUN | Interrupted create with running guest but no init checkpoint remains failed and is never replayed. | `runner/tests/test_automatic_operation_recovery.py::test_restart_preserves_create_ambiguity_without_init_replay` |

---

# QEMU lifecycle verification cases

Plan written before execution of the revised software. All cases start as NOT RUN.
Tests use isolated temporary files, real qemu-img/genisoimage where relevant, libvirt test driver or fault-injected boundaries; actual KVM scenarios are tracked separately by the root verification matrix. PASS does not claim a physical SSD-controller or power-cut experiment.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q001 | Clone publication corruption: hash | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[hash]` | NOT RUN |
| Q002 | Clone publication corruption: size | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[size]` | NOT RUN |
| Q003 | Clone publication corruption: path | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[path]` | NOT RUN |
| Q004 | Clone publication corruption: bytes | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[bytes]` | NOT RUN |
| Q005 | Clone publication corruption: backing | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[backing]` | NOT RUN |
| Q006 | Removal leaves disk | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[disk]` | NOT RUN |
| Q007 | Removal leaves iso | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[iso]` | NOT RUN |
| Q008 | Removal leaves seed | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[seed]` | NOT RUN |
| Q009 | Removal leaves host-key | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[host-key]` | NOT RUN |
| Q010 | Removal leaves symlink | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[symlink]` | NOT RUN |
| Q011 | Removal leaves domain | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[domain]` | NOT RUN |
| Q012 | Removal leaves network | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[network]` | NOT RUN |
| Q013 | Absence observation unavailable: domain | Raise; never claim physical absence | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_never_hides_unavailable_observation[domain]` | NOT RUN |
| Q014 | Absence observation unavailable: network | Raise; never claim physical absence | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_never_hides_unavailable_observation[network]` | NOT RUN |
| Q015 | Absence observation unavailable: storage | Raise; never claim physical absence | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_never_hides_unavailable_observation[storage]` | NOT RUN |
| Q016 | Network allocation observation fails: enumeration | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[enumeration]` | NOT RUN |
| Q017 | Network allocation observation fails: xml | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[xml]` | NOT RUN |
| Q018 | Network allocation observation fails: host | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[host]` | NOT RUN |
| Q019 | Network allocation observation fails: malformed-host | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[malformed-host]` | NOT RUN |
| Q020 | Network allocation observation fails: unknown-prefix | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[unknown-prefix]` | NOT RUN |
| Q021 | Build setting qemu_build_vcpus=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_vcpus]` | NOT RUN |
| Q022 | Build setting qemu_build_vcpus=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_vcpus]` | NOT RUN |
| Q023 | Build setting qemu_build_memory_mb=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_memory_mb]` | NOT RUN |
| Q024 | Build setting qemu_build_memory_mb=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_memory_mb]` | NOT RUN |
| Q025 | Build setting qemu_build_concurrency=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_concurrency]` | NOT RUN |
| Q026 | Build setting qemu_build_concurrency=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_concurrency]` | NOT RUN |
| Q027 | Build setting qemu_build_queue_timeout=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_queue_timeout]` | NOT RUN |
| Q028 | Build setting qemu_build_queue_timeout=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_queue_timeout]` | NOT RUN |
| Q029 | Admission pressure: configured | Temporary capacity exception before allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_memory_pressure_rejects_before_allocating_resources[configured]` | NOT RUN |
| Q030 | Admission pressure: available | Temporary capacity exception before allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_memory_pressure_rejects_before_allocating_resources[available]` | NOT RUN |
| Q031 | create memory pressure | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_memory_rejection_precedes_disk_and_network_allocation[create]` | NOT RUN |
| Q032 | create with noncanonical existing domain | Preserve old domain/disk and reject allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_existing_noncanonical_domain_blocks_fresh_allocation_without_effects[create]` | NOT RUN |
| Q033 | clone memory pressure | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_memory_rejection_precedes_disk_and_network_allocation[clone]` | NOT RUN |
| Q034 | clone with noncanonical existing domain | Preserve old domain/disk and reject allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_existing_noncanonical_domain_blocks_fresh_allocation_without_effects[clone]` | NOT RUN |
| Q035 | Guest boot in progress plus other guest operations | Inventory/other create/start/stop proceed; same guest serializes | `runner/tests/test_qemu_lifecycle_hardening.py::test_guest_readiness_allows_inventory_and_other_guest_lifecycles` | NOT RUN |
| Q036 | Repeated create identity | Existing inode/domain unchanged | `runner/tests/test_qemu_lifecycle_hardening.py::test_duplicate_create_keeps_existing_disk_and_domain` | NOT RUN |
| Q037 | Requested disk smaller than image | Base and clone reject; equal floor succeeds | `runner/tests/test_qemu_lifecycle_hardening.py::test_small_workspace_requests_reject_before_allocating_disk_or_network` | NOT RUN |
| Q038 | Slow SSD publication plus cancellation | Event loop responsive; issued fsync drains before unlock | `runner/tests/test_qemu_lifecycle_hardening.py::test_slow_publication_fsync_keeps_event_loop_responsive_and_drains_cancel` | NOT RUN |
| Q039 | Slow filesystem inventory | Heartbeat task remains schedulable | `runner/tests/test_qemu_lifecycle_hardening.py::test_slow_inventory_filesystem_probe_does_not_block_heartbeat` | NOT RUN |
| Q040 | Child spawned under storage lock | Child must acquire its own mutation lock | `runner/tests/test_qemu_lifecycle_hardening.py::test_child_task_does_not_inherit_storage_mutation_permission` | NOT RUN |
| Q041 | Concurrent build cap exhausted | Second queues without resources, then runs | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_queues_guests_and_uses_separate_cpu_ram` | NOT RUN |
| Q042 | Stopped domains on weak host | No active RAM reservation for stopped guests | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_memory_budget_ignores_stopped_domains` | NOT RUN |
| Q043 | Bootstrap busy / SSH restart | Read-only probes retry; init script not replayed | `runner/tests/test_qemu_lifecycle_hardening.py::test_cloud_init_readonly_probe_retries_running_and_ssh_restart` | NOT RUN |
| Q044 | Bootstrap never completes | Bounded failure, no script/publication, guest cleaned | `runner/tests/test_qemu_lifecycle_hardening.py::test_cloud_init_wait_is_bounded_and_failed_build_never_runs_script` | NOT RUN |
| Q045 | Domain state scan interrupted | Raise, never silently omit a guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_domain_discovery_failure_is_never_authoritative_absence` | NOT RUN |
| Q046 | Two overlay creators same path | One exclusive publication; winner inode preserved | `runner/tests/test_qemu_lifecycle_hardening.py::test_overlay_publication_never_clobbers_a_racing_creator` | NOT RUN |
| Q047 | Cloud cache publication race | Existing cache inode never replaced | `runner/tests/test_qemu_lifecycle_hardening.py::test_cloud_cache_publication_preserves_concurrently_published_inode` | NOT RUN |
| Q048 | Two simultaneous starts fit only one | One activates; other fails before overcommit | `runner/tests/test_qemu_lifecycle_hardening.py::test_concurrent_starts_atomically_reserve_memory_against_other_guests` | NOT RUN |
| Q049 | Resume paused RAM reservation | Existing active reservation counted once | `runner/tests/test_qemu_lifecycle_hardening.py::test_paused_resume_does_not_reserve_existing_guest_ram_twice` | NOT RUN |
| Q050 | Slow durable delete plus cancellation | Deletion drains; event loop stays responsive | `runner/tests/test_qemu_lifecycle_hardening.py::test_slow_image_delete_fsync_is_drained_without_blocking_event_loop` | NOT RUN |
| Q051 | Capture / legacy disk metadata | Actual virtual disk floor propagated | `runner/tests/test_qemu_lifecycle_hardening.py::test_capture_and_legacy_inventory_report_actual_virtual_disk_floor` | NOT RUN |
| Q052 | Temporary RAM shortage | Queues, reports reason, no guest until released | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_waits_for_memory_then_runs_without_allocating_while_queued` | NOT RUN |
| Q053 | Cancel memory waiter | Cancelled waiter allocates no guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_queue_cancel_removes_waiter_without_guest` | NOT RUN |
| Q054 | Cancel concurrency-slot waiter | Only active build executes | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_slot_wait_cancel_never_starts_second_guest` | NOT RUN |
| Q055 | Memory never becomes available | Bounded known failure, no guest/disk/network | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_queue_timeout_is_known_failure_without_allocation` | NOT RUN |
| Q056 | Configured build RAM exceeds physical budget | Immediate clear failure, no endless queue | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_impossible_memory_fails_immediately` | NOT RUN |
| Q057 | Host has fewer CPUs than configured parallel builds | Actual concurrency capped; queued jobs later run | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_cpu_budget_limits_parallelism` | NOT RUN |
| Q058 | Build vCPUs exceeds physical logical CPUs | Immediate clear failure, no guest allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_impossible_cpu_fails_immediately` | NOT RUN |
| Q059 | Memory consumed between queue probe and create | Retry only typed temporary capacity failure | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_rechecks_memory_after_capacity_race` | NOT RUN |
| Q060 | Libvirt inspection or allocation error | Single failure, no unsafe execution replay | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_does_not_retry_unknown_libvirt_failure` | NOT RUN |
| Q061 | Build interrupted / operation identity | Exact deterministic guest ID supports recovery | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_guest_identity_matches_operation` | NOT RUN |
| Q062 | Malformed operation identity | Reject before cache/network/disk work | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_invalid_operation_identity_has_no_effects` | NOT RUN |
| Q063 | Progress delivery fails before admission | No guest mutation; clear failure | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_queue_progress_failure_does_not_allocate` | NOT RUN |
| Q064 | Cancel after guest activated | Drain issued effects/cleanup before releasing slot | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_cancel_during_effect_drains_and_releases_slot` | NOT RUN |
| Q065 | Remove guest state=1, persistent=True | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[1-True]` | NOT RUN |
| Q066 | Remove guest state=3, persistent=True | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[3-True]` | NOT RUN |
| Q067 | Remove guest state=3, persistent=False | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[3-False]` | NOT RUN |
| Q068 | Remove guest state=5, persistent=True | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[5-True]` | NOT RUN |
| Q069 | Remove failure: destroy | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[destroy]` | NOT RUN |
| Q070 | Remove failure: undefine | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[undefine]` | NOT RUN |
| Q071 | Remove failure: present | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[present]` | NOT RUN |
| Q072 | Remove failure: enumeration | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[enumeration]` | NOT RUN |
| Q073 | Remove state=1, blocker=foreign | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[1-foreign]` | NOT RUN |
| Q074 | Remove state=1, blocker=overlay | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[1-overlay]` | NOT RUN |
| Q075 | Remove state=1, blocker=partial | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[1-partial]` | NOT RUN |
| Q076 | Remove state=3, blocker=foreign | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[3-foreign]` | NOT RUN |
| Q077 | Remove state=3, blocker=overlay | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[3-overlay]` | NOT RUN |
| Q078 | Remove state=3, blocker=partial | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[3-partial]` | NOT RUN |
| Q079 | Capture source running / unscrubbed / ready | Only stopped scrubbed source publishes standalone exclusive artifact | `runner/tests/test_storage_inventory.py::test_capture_self_contained_stopped_scrubbed_and_exclusive` | NOT RUN |
| Q080 | Image has descendant or unreadable graph | Deletion blocked safely | `runner/tests/test_storage_inventory.py::test_capture_descendant_and_unreadable_scan_block_delete` | NOT RUN |
| Q081 | Crash / ENOSPC / invalid publication target | No false-ready marker or arbitrary path write | `runner/tests/test_storage_inventory.py::test_publication_crash_marker_enospc_and_invalid_path` | NOT RUN |
| Q082 | Physical remove error | Disk retained for retry | `runner/tests/test_storage_inventory.py::test_failed_remove_retains_disk` | NOT RUN |
| Q083 | Cancellation while threaded effect outstanding | Mutation lock retained until effect settles | `runner/tests/test_storage_inventory.py::test_mutation_cancellation_drains_thread_and_holds_lock` | NOT RUN |
| Q084 | Published artifact plus lost acknowledgement | Recover exact publication; no rebuild | `runner/tests/test_storage_inventory.py::test_journal_recovers_exact_publication_without_rerun` | NOT RUN |
| Q085 | Partial create removal | Only exact owned partial resources removed | `runner/tests/test_storage_inventory.py::test_explicit_partial_create_removal_preserves_unrelated_disk` | NOT RUN |
| Q086 | Graph changes while removing domain | Recheck dependencies before unlink | `runner/tests/test_storage_inventory.py::test_workspace_delete_rechecks_after_undefine` | NOT RUN |
| Q087 | Corrupt output versus changed source | Source proof retained only for unchanged source | `runner/tests/test_storage_inventory.py::test_source_scrub_survives_corrupt_output_but_not_external_disk_write` | NOT RUN |
| Q088 | Image conversion truncated | No usable publication; source evidence remains | `runner/tests/test_storage_inventory.py::test_truncated_conversion_failure_retains_safe_source_evidence` | NOT RUN |
| Q089 | Same-size/mtime external artifact mutation | Hash cache invalidates using inode metadata | `runner/tests/test_storage_inventory.py::test_publication_hash_cache_invalidates_same_size_and_mtime` | NOT RUN |
| Q090 | Published path replaced externally | Foreign replacement not deleted | `runner/tests/test_storage_inventory.py::test_cached_publication_foreign_overwrite_cannot_be_deleted` | NOT RUN |
| Q091 | Unpublished output exists | Dependency protects parent; output not cloneable | `runner/tests/test_storage_inventory.py::test_unpublished_images_keep_physical_dependencies_but_cannot_be_used` | NOT RUN |
| Q092 | Owned and orphan cloud-init files | Inventory maps exact ownership only | `runner/tests/test_storage_inventory.py::test_cloud_init_inventory_exact_ownership_and_orphans` | NOT RUN |
| Q093 | Delete original captured source | Standalone artifact remains cloneable | `runner/tests/test_storage_inventory.py::test_standalone_capture_survives_origin_disk_and_seed_removal` | NOT RUN |
| Q094 | Two parallel physical QCOW2/ISO builds | Distinct guests/subnets, complete inventory, clean teardown | `runner/tests/test_parallel_image_builds.py::test_parallel_builds_have_distinct_networks_and_inventory_stays_available` | NOT RUN |
| Q095 | One parallel build init fails | Other succeeds; both guests cleaned | `runner/tests/test_parallel_image_builds.py::test_one_failed_build_is_cleaned_without_aborting_another` | NOT RUN |
| Q096 | Cancel running build with inventory probe | Build drains cleanup; unrelated inventory available | `runner/tests/test_parallel_image_builds.py::test_caller_cancellation_drains_build_and_cleanup_without_global_lock` | NOT RUN |

---

# Real VM and user-state verification plan

Recorded before execution. `V001`–`V032` use Ubuntu 22.04; `V033`–`V064`
repeat the same scenarios using Ubuntu 24.04. All use real KVM/libvirt, QCOW2,
SSH and cloud-init. Network/storage fault injection is labelled explicitly;
it does not claim to reproduce a physical SSD controller failure.

| IDs (22.04 / 24.04) | Scenario → expected outcome | Status |
|---|---|---|
| V001 / V033 | Two distinct image builds → overlapping initialization if admitted by host RAM/CPU. | NOT RUN |
| V002 / V034 | Third build at concurrency limit → queued; no temporary guest allocated yet. | NOT RUN |
| V003 / V035 | Workspace create while builds initialize → independently completes with correct image. | NOT RUN |
| V004 / V036 | Workspace CPU/RAM/disk update while builds run → completes; data preserved. | NOT RUN |
| V005 / V037 | Workspace stop during builds → stops; builders continue. | NOT RUN |
| V006 / V038 | Queued build after a slot frees → automatically starts; all three build results correct. | NOT RUN |
| V007 / V039 | Published definition images → standalone QCOW2, valid hash/identity/minimum disk manifest. | NOT RUN |
| V008 / V040 | Inventory after builds → complete, published images present, temporary builders absent. | NOT RUN |
| V009 / V041 | Definition-backed workspace → correct recipe marker; cloud-init complete. | NOT RUN |
| V010 / V042 | Backend unavailable during create → VM completes; terminal result retained durably. | NOT RUN |
| V011 / V043 | Reconnect, result replay and ACK → same operation; no pending result after matching ACK. | NOT RUN |
| V012 / V044 | Duplicate durable/raw create → existing guest and disk inode/data preserved. | NOT RUN |
| V013 / V045 | Controlled stop after injected test credential → stopped; credential scrub proven. | NOT RUN |
| V014 / V046 | Capture stopped clean guest → standalone image with source identity and disk floor. | NOT RUN |
| V015 / V047 | Clone capture and resume source concurrently → both running; persisted marker preserved. | NOT RUN |
| V016 / V048 | Clone below image disk floor → rejected before domain/network/disk allocation. | NOT RUN |
| V017 / V049 | Delete image with live clone dependency → refused; clone data readable. | NOT RUN |
| V018 / V050 | Tampered publication hash → clone refused; no guest allocation. | NOT RUN |
| V019 / V051 | Live CPU/RAM/disk growth → XML/settings reflect request; persisted file survives. | NOT RUN |
| V020 / V052 | Disk shrink request → never shrinks existing disk or loses data. | NOT RUN |
| V021 / V053 | Impossible RAM request → clear failure before restart/storage mutation; guest remains usable. | NOT RUN |
| V022 / V054 | Same-guest stop/resume submitted concurrently → serialized completion; final running state. | NOT RUN |
| V023 / V055 | Externally paused guest stop → actual shutoff, no false scrub proof. | NOT RUN |
| V024 / V056 | Resume stopped guest → readable persisted data; controlled stop restores scrub proof. | NOT RUN |
| V025 / V057 | Delete capture origin → captured clone remains usable and independent. | NOT RUN |
| V026 / V058 | Recreate physically removed ID → new incarnation; no stale storage reuse. | NOT RUN |
| V027 / V059 | Repeated exact removal → absent domain/network/disk/seed/host-key; idempotent success. | NOT RUN |
| V028 / V060 | Remove last clone then image → succeeds; no physical image dependency remains. | NOT RUN |
| V029 / V061 | Init script exits nonzero → failed build; no published image; temporary guest cleaned. | NOT RUN |
| V030 / V062 | Build after failed build → queue slot released; next image publishes successfully. | NOT RUN |
| V031 / V063 | Runner SIGKILL during workspace initialization → restart seals failure; initialization never replayed. | NOT RUN |
| V032 / V064 | Recovered partial workspace → exact cleanup succeeds without admin acknowledgement. | NOT RUN |

Additional final-code scenarios, each with its own evidence:

| ID | Scenario → expected outcome | Verification | Status |
|---|---|---|---|
| V065 | Inject temporary host RAM pressure with real VM build → queue without allocation; automatically build when pressure removed. | VM harness fault mode | NOT RUN |
| V066 | Runner SIGKILL during real image initialization → exact orphan builder stopped/removed; failed retryable operation; recipe not replayed. | VM harness crash-build mode | NOT RUN |
| V067 | Real guest disk write throttle → slow write completes; event loop and unrelated guest operation responsive. | libvirt blockIoTune + SSH | NOT RUN |
| V068 | Guest abruptly powered off → actual stopped state discovered; resume and controlled stop possible. | libvirt destroy + production runtime | NOT RUN |
| V069 | Real publication, result not ACKed, runner restarted → successful exact result recovered without rebuild. | Real QCOW2 + SQLite journal restart | NOT RUN |
| V070 | Real loopback backend stopped during lifecycle → completes offline; reconnect/ACK without duplicate execution. | `test_lifecycle_transport_integration.py` | NOT RUN |
| V071 | Tiny real filesystem filled → failed QCOW2 write does not replace existing publication. | isolated small filesystem fault | NOT RUN |
| V072 | Libvirt inventory unavailable → incomplete observation never proves absence or allows destructive deletion. | QEMU inventory fault regression | NOT RUN |
| U001 | Workspace list and deletion failures → clear status, deleted entries hidden. | WorkspacesView tests | NOT RUN |
| U002 | Stop/start/capture/reset action combinations → conflict/offline/recovery reservations disable unsafe actions. | WorkspaceActions tests | NOT RUN |
| U003 | Workspace operation events → pending operations clear only on matching terminal state. | workspace store tests | NOT RUN |
| U004 | Chat workspace readiness across statuses → no launch on deleted/offline/blocked workspace. | conversationGroups/readiness tests | NOT RUN |
| U005 | Image definition states → deletion/build conflicts and retry actions clear. | imageDefinitionLifecycle tests | NOT RUN |
| U006 | Interrupted operation inspector → exact evidence and explicit permitted actions only. | RunnerOperationsPanel tests | NOT RUN |
| U007 | Workspace configuration/reset dialogs → image readiness/minimum/resource choices validated. | WorkspaceRecreateDialog tests | NOT RUN |
| U008 | Frontend after removal of duplicate deleted state → type check succeeds. | webapp type-check | NOT RUN |

Physical host power loss, defective RAM and SSD firmware/controller failure cannot
be safely induced in this shared VM. Software boundaries are exercised with
process kills, delayed IO, full filesystems, inspection failures and transport
outages. Such injected tests are never labelled physical hardware tests.
