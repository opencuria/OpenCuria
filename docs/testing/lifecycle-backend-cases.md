# Backend lifecycle test cases

Plan recorded before test execution. Every row is initially NOT RUN. PASS requires the named assertion to execute successfully against the final code; unit/database simulation is explicitly distinguished from real VM tests in the consolidated report.

| ID | Scenario and expected outcome | Verification | Status |
|---|---|---|---|
| B001 | create_workspace, sealed interrupted journal, runtime running: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace-running]` | PASS |
| B002 | create_workspace, sealed interrupted journal, runtime stopped: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace-stopped]` | PASS |
| B003 | create_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace-absent]` | PASS |
| B004 | create_workspace_from_image_artifact, sealed interrupted journal, runtime running: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace_from_image_artifact-running]` | PASS |
| B005 | create_workspace_from_image_artifact, sealed interrupted journal, runtime stopped: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace_from_image_artifact-stopped]` | PASS |
| B006 | create_workspace_from_image_artifact, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_workspace_from_image_artifact-absent]` | PASS |
| B007 | stop_workspace, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[stop_workspace-running]` | PASS |
| B008 | stop_workspace, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[stop_workspace-stopped]` | PASS |
| B009 | stop_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[stop_workspace-absent]` | PASS |
| B010 | resume_workspace, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[resume_workspace-running]` | PASS |
| B011 | resume_workspace, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[resume_workspace-stopped]` | PASS |
| B012 | resume_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[resume_workspace-absent]` | PASS |
| B013 | update_workspace, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[update_workspace-running]` | PASS |
| B014 | update_workspace, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[update_workspace-stopped]` | PASS |
| B015 | update_workspace, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[update_workspace-absent]` | PASS |
| B016 | remove_workspace, sealed interrupted journal, runtime running: release only this fence; delete_failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[remove_workspace-running]` | PASS |
| B017 | remove_workspace, sealed interrupted journal, runtime stopped: release only this fence; delete_failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[remove_workspace-stopped]` | PASS |
| B018 | remove_workspace, sealed interrupted journal, runtime absent: release only this fence; delete_failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[remove_workspace-absent]` | PASS |
| B019 | build_image, sealed interrupted journal, runtime running: release only this fence; failed generation / prior current preserved; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[build_image-running]` | PASS |
| B020 | build_image, sealed interrupted journal, runtime stopped: release only this fence; failed generation / prior current preserved; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[build_image-stopped]` | PASS |
| B021 | build_image, sealed interrupted journal, runtime absent: release only this fence; failed generation / prior current preserved; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[build_image-absent]` | PASS |
| B022 | create_image_artifact, sealed interrupted journal, runtime running: release only this fence; running; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_image_artifact-running]` | PASS |
| B023 | create_image_artifact, sealed interrupted journal, runtime stopped: release only this fence; stopped; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_image_artifact-stopped]` | PASS |
| B024 | create_image_artifact, sealed interrupted journal, runtime absent: release only this fence; failed; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[create_image_artifact-absent]` | PASS |
| B025 | delete_image, sealed interrupted journal, runtime running: release only this fence; delete_failed / retry available; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[delete_image-running]` | PASS |
| B026 | delete_image, sealed interrupted journal, runtime stopped: release only this fence; delete_failed / retry available; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[delete_image-stopped]` | PASS |
| B027 | delete_image, sealed interrupted journal, runtime absent: release only this fence; delete_failed / retry available; no script replay or resource deletion. | `test_automatic_recovery_matrix.py::test_interrupted_operation_matrix[delete_image-absent]` | PASS |
| B028 | Unsafe evidence runner: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[runner]` | PASS |
| B029 | Unsafe evidence operation_id: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[operation_id]` | PASS |
| B030 | Unsafe evidence attempt: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[attempt]` | PASS |
| B031 | Unsafe evidence target: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[target]` | PASS |
| B032 | Unsafe evidence instance: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[instance]` | PASS |
| B033 | Unsafe evidence finished: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[finished]` | PASS |
| B034 | Unsafe evidence quiescent: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[quiescent]` | PASS |
| B035 | Unsafe evidence running: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[running]` | PASS |
| B036 | Unsafe evidence unsealed: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[unsealed]` | PASS |
| B037 | Unsafe evidence known_unfinished: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[known_unfinished]` | PASS |
| B038 | Unsafe evidence missing_outcome: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[missing_outcome]` | PASS |
| B039 | Unsafe evidence replaced_session: do not close operation; preserve task and workspace reservation. | `test_automatic_recovery_matrix.py::test_unsafe_inspection_matrix[replaced_session]` | PASS |
| B040 | create_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[create_workspace]` | PASS |
| B041 | create_workspace_from_image_artifact, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[create_workspace_from_image_artifact]` | PASS |
| B042 | stop_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[stop_workspace]` | PASS |
| B043 | resume_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[resume_workspace]` | PASS |
| B044 | update_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[update_workspace]` | PASS |
| B045 | remove_workspace, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[remove_workspace]` | PASS |
| B046 | build_image, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[build_image]` | PASS |
| B047 | create_image_artifact, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[create_image_artifact]` | PASS |
| B048 | delete_image, known terminal success: route to exact callback; accept result once; replay cannot alter newer operation. | `test_automatic_recovery_matrix.py::test_known_inspection_dispatch[delete_image]` | PASS |
| B049 | create_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[create_workspace]` | PASS |
| B050 | create_workspace_from_image_artifact, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[create_workspace_from_image_artifact]` | PASS |
| B051 | stop_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[stop_workspace]` | PASS |
| B052 | resume_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[resume_workspace]` | PASS |
| B053 | update_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[update_workspace]` | PASS |
| B054 | remove_workspace, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[remove_workspace]` | PASS |
| B055 | build_image, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[build_image]` | PASS |
| B056 | create_image_artifact, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[create_image_artifact]` | PASS |
| B057 | delete_image, intent allocated but never prepared: bounded failure releases reservation; never sends empty identity; no operator acknowledgement. | `test_automatic_recovery_matrix.py::test_unprepared_intent_matrix[delete_image]` | PASS |
| B058 | create_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[create_workspace]` | PASS |
| B059 | create_workspace_from_image_artifact, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[create_workspace_from_image_artifact]` | PASS |
| B060 | stop_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[stop_workspace]` | PASS |
| B061 | resume_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[resume_workspace]` | PASS |
| B062 | update_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[update_workspace]` | PASS |
| B063 | remove_workspace, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[remove_workspace]` | PASS |
| B064 | build_image, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[build_image]` | PASS |
| B065 | create_image_artifact, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[create_image_artifact]` | PASS |
| B066 | delete_image, fresh execution/queue heartbeat after old deadline: continue waiting, no failure or duplicate delivery; renew execution deadline. | `test_automatic_recovery_matrix.py::test_live_heartbeat_matrix[delete_image]` | PASS |
| B067 | create_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[create_workspace]` | PASS |
| B068 | create_workspace_from_image_artifact, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[create_workspace_from_image_artifact]` | PASS |
| B069 | stop_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[stop_workspace]` | PASS |
| B070 | resume_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[resume_workspace]` | PASS |
| B071 | update_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[update_workspace]` | PASS |
| B072 | remove_workspace, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[remove_workspace]` | PASS |
| B073 | build_image, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[build_image]` | PASS |
| B074 | create_image_artifact, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[create_image_artifact]` | PASS |
| B075 | delete_image, delivered attempt deadline expired with no heartbeat: retain ambiguous identity until fresh inspection, never replay arbitrary scripts under a new identity. | `test_automatic_recovery_matrix.py::test_stale_heartbeat_matrix[delete_image]` | PASS |
| B076 | stop_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[stop_workspace]` | PASS |
| B077 | resume_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[resume_workspace]` | PASS |
| B078 | update_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[update_workspace]` | PASS |
| B079 | remove_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[remove_workspace]` | PASS |
| B080 | create_workspace, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[create_workspace]` | PASS |
| B081 | create_image_artifact, newer workspace task owns resource: late inspection cannot release or overwrite newer owner. | `test_automatic_recovery_matrix.py::test_newer_owner_matrix[create_image_artifact]` | PASS |
| B082 | capture-stop, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[capture-stop]` | PASS |
| B083 | capture-image, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[capture-image]` | PASS |
| B084 | recreate-remove, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[recreate-remove]` | PASS |
| B085 | recreate-create, interrupted child: parent closes as failed with preserved resources and source reservation released; explicit retry available. | `test_automatic_recovery_matrix.py::test_parent_interruption_matrix[recreate-create]` | PASS |
| B086 | Credential initial=False, proof=None: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[False-None]` | PASS |
| B087 | Credential initial=False, proof=False: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[False-False]` | PASS |
| B088 | Credential initial=False, proof=True: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[False-True]` | PASS |
| B089 | Credential initial=True, proof=None: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[True-None]` | PASS |
| B090 | Credential initial=True, proof=False: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[True-False]` | PASS |
| B091 | Credential initial=True, proof=True: preserve previous presence unless exact current-runtime boolean proof exists. | `test_automatic_recovery_matrix.py::test_credential_proof_matrix[True-True]` | PASS |
| B092 | Interrupted build while prior current image exists: failed pending generation never replaces current image; retry allocates new generation. | `test_interrupted_builds.py::test_interruption_and_explicit_close_reconcile_build_assignment` | PASS |
| B093 | Capture closure keeps ready/deleting images and marks unfinished reservation failed. | `test_capture_disposition.py` | PASS |
| B094 | Unknown outcome is not represented as execution success; diagnostic explains interruption and safe retry, not admin work. | `test_automatic_recovery_matrix.py::test_user_diagnostic` | PASS |
| B095 | Never-delivered offline prepared operation retains original identity beyond deadline and continues after reconnect. | `test_automatic_recovery_matrix.py::test_never_delivered_offline_stays_queued` | PASS |
| B096 | Unreproducible credential payload before first delivery fails locally and releases workspace fence without disclosing secrets. | `test_automatic_recovery_matrix.py::test_unreproducible_input_fails_without_fence` | PASS |
| B097 | Full backend runner suite SQLite: all relevant lifecycle, auth, inventory, recipes, settings and migrations remain green. | `pytest apps/runners/tests (SQLite)` | PASS |
| B098 | Full backend runner suite PostgreSQL: real row locks, CAS races, simultaneous allocation and historical migrations remain green. | `pytest apps/runners/tests (PostgreSQL)` | PASS |

Additional cases recorded before their first execution (the original frozen plan remains unchanged):

| ID | Scenario and expected outcome | Verification | Status |
|---|---|---|---|
| B099 | Legacy removed workspace status migrates idempotently to deleted; bytes/credential evidence remain unchanged. | `test_removed_migration_normalizes_only_terminal_label` | PASS |
| B100 | Immediate dispatch persists delivery count and lease before emit; recovery cannot duplicate active lease. | `test_direct_delivery_boundary` | PASS |
| B101 | Immediate dispatch rejects completed task and uses current authenticated runner SID. | `test_direct_delivery_uses_current_session` | PASS |
| B102 | Sealed unknown deletion automatically retries with fresh inventory; no admin acknowledgement required. | `test_sealed_deletion_retry` | PASS |
| B103 | Repeated deletion failure ends in failed; another ordinary deletion request can retry with original approval. | `test_exhausted_deletion_retry` | PASS |
| B104 | Failure text mentioning intervention never creates implicit state/fence. | `test_operation_results.py::test_failure_message_does_not_create_a_hidden_intervention_fence` | PASS |

Continuation cases recorded before execution (2026-10-31):

| ID | Scenario and expected outcome | Verification | Status |
|---|---|---|---|
| B105 | Interrupted create with retained running guest: failure releases workflow fence but keeps CPU, RAM and disk reservation until fresh stopped/absent evidence. | `test_failed_guest_resource_reservation[*-running]` | PASS |
| B106 | Interrupted create with retained stopped guest: failure keeps disk reservation while CPU/RAM become available. | `test_failed_guest_resource_reservation[*-stopped]` | PASS |
| B107 | Interrupted create with unknown partial resources: ordinary delete remains available; conservative disk/compute reservation prevents oversubscription. | `test_failed_guest_resource_reservation[*-unknown]` | PASS |
| B108 | Interrupted create with complete absence proof: release all capacity without admin acknowledgement. | `test_failed_guest_resource_reservation[*-absent]` | PASS |
| B109 | Stopped workspace disk still consumes aggregate disk capacity; CPU/RAM can be reused. | `test_stopped_workspace_retains_disk_capacity` | PASS |
| B110 | Fresh initial-create preparation failure proves no VM was started and frees all capacity. | `test_unissued_failure_releases_all_capacity` | PASS |
| B111 | Confirmed deleted workspace consumes no capacity even with historic retained-resource evidence. | `test_deleted_workspace_releases_retained_capacity` | PASS |
| B112 | Known failed deletion retains physical resources and cannot open a new aggregate-capacity slot. | `test_delete_failed_retains_resource_capacity` | PASS |
| B113 | Runtime observations used only for accounting never leak into runner command fingerprints or retries. | `test_resource_observation_is_backend_only` | PASS |
| B114 | A fresh operation supersedes historic absence evidence; a later failed guest keeps its own conservative reservation. | `test_new_attempt_does_not_inherit_absence_proof` | PASS |
| B115 | Terminal runner result records stopped/absent/running evidence for accounting; failed stop with stopped evidence releases compute only. | `test_terminal_result_records_runtime_evidence` | PASS |
| B116 | Interrupted capture resume: automatic recovery fails parent and preserves already-ready image, source disk and explicit retry path. | `test_parent_interruption_matrix[capture-resume]` | PASS |
| B117 | Interrupted recreate final stop: automatic recovery closes parent, releases exact child and keeps successfully provisioned workspace resources. | `test_parent_interruption_matrix[recreate-stop]` | PASS |
| B118 | Complete fresh inventory with invalid publication/hash metadata at the exact known artifact path: prior ready image becomes failed and is unselectable; physical graph stays available. | `test_invalid_publication_disables_exact_ready_image` | PASS |
| B119 | Partial/stale/foreign/unmanaged/ambiguous invalid-publication reports cannot change publication readiness of another image. | `test_untrusted_publication_observation_cannot_mutate_image` | PASS |
| B120 | Corrupt artifact still has its existing workspace pin and physical backing edges; no deletion authority is inferred from invalid bytes. | `test_corrupt_publication_preserves_workspace_dependencies` | PASS |
| B121 | Stopped resource settings reserve future disk allocation atomically, while saved CPU/RAM do not consume running capacity; invalid disk growth rolls back unchanged settings. | `test_stopped_settings_reserve_disk_without_compute` | PASS |
| B122 | All 9 operation kinds with unknown runtime observation or exited alias: close only proven quiescent exact identity; unknown stays failed/conservative, exited normalizes stopped, image outcomes retain their type-specific failure behavior. | `test_interrupted_operation_matrix[*-unknown/exited]` | PASS |

## State axes and reachable combinations reviewed

- Operation kind: all 9 durable lifecycle commands; parent phase: capture stop/capture/resume and recreate remove/create/stop.
- Task outcome: pending/in_progress/completed/failed; delivery phase: intent/delivery/executing/result/intervention/disposed. `intervention` is an internal retained execution fence awaiting automatic inspection, not a required administrator acknowledgement.
- Delivery evidence: never issued/prepared/delivered/live heartbeat/stale heartbeat/expired deadline; runner: current online session/offline/stale heartbeat/replaced SID.
- Observation: running/stopped/exited/absent/unknown; execution proof: exact vs mismatched operation/runner/attempt/target, sealed/unsealed, finished/unfinished, quiescent/active, known/unknown outcome. Unknown power state is never inferred as successful initialization.
- Ownership: exact child/current workflow/newer task/already released; image: prior current/pending generation/ready/corrupt/retiring/deleting/deleted. Credential presence changes only on explicit boolean evidence from the exact current attempt.
- Capacity: each of CPU/RAM/disk at admission limits; parallel create/resume/update/mixed admissions; interrupted retained guest; stopped disk; complete absence; old sizing after failed downsize; fresh settings after a new attempt.
- Invalid or stale identity/session/proof combinations leave the exact reservation intact and cannot mutate a newer owner. Proven interrupted attempts close as failed history and release their workflow fence automatically; arbitrary initialization scripts are not replayed under a new identity. Known terminal results reconcile idempotently.
- The catalog is a finite reachable-state and failure-equivalence matrix, supplemented by the complete runner-app suites on SQLite and PostgreSQL. It does not claim every possible hardware device, kernel failure or arbitrary Cartesian database corruption was physically exercised; real VM/hardware boundaries are listed separately in the VM report.

## Final review cases recorded before execution

| ID | Scenario and expected outcome | Verification | Status |
|---|---|---|---|
| B123 | Never-issued stop/resume/update/remove/capture or incompatible create intent on existing running/stopped guest cannot assert resource absence; retain guest/disk reservations after local failure. | `test_unissued_existing_guest_never_claims_absence` | PASS |
| B124 | Running/stopped disk shrink request fails under lifecycle locks before any settings/task mutation. | `test_disk_settings_cannot_shrink_retained_volume` | PASS |
| B125 | Historic desired disk smaller than last confirmed runtime disk: admission retains confirmed physical size and prevents over-allocation. | `test_confirmed_disk_floor_survives_legacy_desired_shrink` | PASS |
| B126 | Local never-issued removal fails to delete_failed and releases task; ordinary removal remains retryable, retained resources are charged. | `test_unissued_remove_is_failed_and_retryable` | PASS |
| B127 | Nullable legacy guest fields use last confirmed sizing before changed runner defaults; resume materializes explicit sizes before durable preparation and dispatch. | `test_legacy_null_resources_keep_confirmed_sizes_on_resume` | PASS |

## Explicit runtime observations recorded before execution

| ID | Scenario and expected outcome | Verification | Status |
|---|---|---|---|
| B128 | Explicit paused/suspended/stopping/unknown/crashed/invalid observation on idle running guest becomes failed, clears desktop availability and retains resources/credentials; never stays chat-ready. | `test_unusable_runtime_observation_is_failed_and_keeps_resources` | PASS |
| B129 | BLOCKED guest is still runnable; running heartbeat reconciliation remains available. | `test_blocked_runtime_observation_remains_running` | PASS |
| B130 | Missing workspace row/failed scan leaves prior state unchanged; explicit unusable observation cannot overwrite lifecycle-owned transition. | `test_missing_or_owned_runtime_observation_does_not_override_state` | PASS |

## Final execution evidence

- 134/134 case groups PASS. Complete baseline (1,522 JUnit nodes each): SQLite 1,501 PASS / 21 PostgreSQL-only SKIP; PostgreSQL 1,521 PASS / 1 SQLite-only SKIP. B131–B134 followed as a bounded four-file correction; final focused image/inventory/API/deletion/contention/architecture suites (180 nodes each): SQLite 161 PASS / 19 PostgreSQL-only SKIP; PostgreSQL 180 PASS. Every skipped node passed on the other database. This does not claim another complete suite on the final four-file delta.
- Explicit case-ID to executed node/outcome mapping: [coverage.json](lifecycle-evidence/backend/coverage.json). Portable counts, source hashes and cleanup summaries (raw commands and logs remain workspace-local): [backend review](lifecycle-evidence/backend/review.md).
- The original pre-execution plan remains preserved at [plan-before-execution.md](lifecycle-evidence/plan-before-execution.md).

## Current publication projection recorded before execution

| ID | Scenario and expected outcome | Verification | Status |
|---|---|---|---|
| B131 | Fresh exact invalid current build publication: image and idle active assignment become failed; current pointer, successful task history and build log stay intact; raw/annotated summaries show failed rather than active. | `test_corrupt_current_publication_fails_assignment_and_summary` | PASS |
| B132 | Corruption of an older/current publication cannot overwrite newer pending/building/current work or deletion/deactivated assignment state/pointers. | `test_corrupt_publication_preserves_newer_or_inactive_assignment` | PASS |
| B133 | Ordinary rebuild from corrupt current publication enters pending, publishes a fresh active generation, preserves old resources/pins, and rejects stale old completion/failure callbacks. | `test_corrupt_current_can_rebuild_without_stale_result_promotion` | PASS |
| B134 | Corruption discovered before/during a newer build: if that build fails, assignment becomes failed with unavailable old current retained; another ordinary retry publishes successfully, with old callbacks rejected. | `test_failed_rebuild_with_corrupt_current_remains_retryable` | PASS |
