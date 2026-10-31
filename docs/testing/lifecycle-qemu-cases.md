# QEMU lifecycle verification cases

Plan written before execution of the revised software. All cases start as NOT RUN.
Tests use isolated temporary files, real qemu-img/genisoimage where relevant, libvirt test driver or fault-injected boundaries; actual KVM scenarios are tracked separately by the root verification matrix. PASS does not claim a physical SSD-controller or power-cut experiment.

Verification: **all catalog cases PASS**, from the final runner suite (**1364 passed, 8 optional unrelated skips, 36 subtests passed**, 152.80 s). [JUnit evidence](lifecycle-evidence/runner/full-fresh-cache-final.xml), [suite summary](lifecycle-evidence/runner/suite-summary.json). Exact case-to-JUnit expansion is stored in [catalog-coverage.json](lifecycle-evidence/runner/catalog-coverage.json).

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q001 | Clone publication corruption: hash | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[hash]` | PASS |
| Q002 | Clone publication corruption: size | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[size]` | PASS |
| Q003 | Clone publication corruption: path | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[path]` | PASS |
| Q004 | Clone publication corruption: bytes | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[bytes]` | PASS |
| Q005 | Clone publication corruption: backing | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_clone_rejects_invalid_publication_before_allocating_guest[backing]` | PASS |
| Q006 | Removal leaves disk | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[disk]` | PASS |
| Q007 | Removal leaves iso | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[iso]` | PASS |
| Q008 | Removal leaves seed | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[seed]` | PASS |
| Q009 | Removal leaves host-key | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[host-key]` | PASS |
| Q010 | Removal leaves symlink | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[symlink]` | PASS |
| Q011 | Removal leaves domain | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[domain]` | PASS |
| Q012 | Removal leaves network | Exact absence remains false | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_requires_every_canonical_resource_gone[network]` | PASS |
| Q013 | Absence observation unavailable: domain | Raise; never claim physical absence | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_never_hides_unavailable_observation[domain]` | PASS |
| Q014 | Absence observation unavailable: network | Raise; never claim physical absence | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_never_hides_unavailable_observation[network]` | PASS |
| Q015 | Absence observation unavailable: storage | Raise; never claim physical absence | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_absence_never_hides_unavailable_observation[storage]` | PASS |
| Q016 | Network allocation observation fails: enumeration | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[enumeration]` | PASS |
| Q017 | Network allocation observation fails: xml | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[xml]` | PASS |
| Q018 | Network allocation observation fails: host | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[host]` | PASS |
| Q019 | Network allocation observation fails: malformed-host | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[malformed-host]` | PASS |
| Q020 | Network allocation observation fails: unknown-prefix | No subnet/domain definition from incomplete data | `runner/tests/test_qemu_lifecycle_hardening.py::test_subnet_allocation_fails_closed_on_incomplete_network_observation[unknown-prefix]` | PASS |
| Q021 | Build setting qemu_build_vcpus=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_vcpus]` | PASS |
| Q022 | Build setting qemu_build_vcpus=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_vcpus]` | PASS |
| Q023 | Build setting qemu_build_memory_mb=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_memory_mb]` | PASS |
| Q024 | Build setting qemu_build_memory_mb=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_memory_mb]` | PASS |
| Q025 | Build setting qemu_build_concurrency=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_concurrency]` | PASS |
| Q026 | Build setting qemu_build_concurrency=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_concurrency]` | PASS |
| Q027 | Build setting qemu_build_queue_timeout=0 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[0-qemu_build_queue_timeout]` | PASS |
| Q028 | Build setting qemu_build_queue_timeout=-1 | Configuration validation rejects | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_rejects_non_positive_values[-1-qemu_build_queue_timeout]` | PASS |
| Q029 | Admission pressure: configured | Temporary capacity exception before allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_memory_pressure_rejects_before_allocating_resources[configured]` | PASS |
| Q030 | Admission pressure: available | Temporary capacity exception before allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_memory_pressure_rejects_before_allocating_resources[available]` | PASS |
| Q031 | create memory pressure | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_memory_rejection_precedes_disk_and_network_allocation[create]` | PASS |
| Q032 | create with noncanonical existing domain | Preserve old domain/disk and reject allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_existing_noncanonical_domain_blocks_fresh_allocation_without_effects[create]` | PASS |
| Q033 | clone memory pressure | Reject before disk/network allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_workspace_memory_rejection_precedes_disk_and_network_allocation[clone]` | PASS |
| Q034 | clone with noncanonical existing domain | Preserve old domain/disk and reject allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_existing_noncanonical_domain_blocks_fresh_allocation_without_effects[clone]` | PASS |
| Q035 | Guest boot in progress plus other guest operations | Inventory/other create/start/stop proceed; same guest serializes | `runner/tests/test_qemu_lifecycle_hardening.py::test_guest_readiness_allows_inventory_and_other_guest_lifecycles` | PASS |
| Q036 | Repeated create identity | Existing inode/domain unchanged | `runner/tests/test_qemu_lifecycle_hardening.py::test_duplicate_create_keeps_existing_disk_and_domain` | PASS |
| Q037 | Requested disk smaller than image | Base and clone reject; equal floor succeeds | `runner/tests/test_qemu_lifecycle_hardening.py::test_small_workspace_requests_reject_before_allocating_disk_or_network` | PASS |
| Q038 | Slow SSD publication plus cancellation | Event loop responsive; issued fsync drains before unlock | `runner/tests/test_qemu_lifecycle_hardening.py::test_slow_publication_fsync_keeps_event_loop_responsive_and_drains_cancel` | PASS |
| Q039 | Slow filesystem inventory | Heartbeat task remains schedulable | `runner/tests/test_qemu_lifecycle_hardening.py::test_slow_inventory_filesystem_probe_does_not_block_heartbeat` | PASS |
| Q040 | Child spawned under storage lock | Child must acquire its own mutation lock | `runner/tests/test_qemu_lifecycle_hardening.py::test_child_task_does_not_inherit_storage_mutation_permission` | PASS |
| Q041 | Concurrent build cap exhausted | Second queues without resources, then runs | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_budget_queues_guests_and_uses_separate_cpu_ram` | PASS |
| Q042 | Stopped domains on weak host | No active RAM reservation for stopped guests | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_memory_budget_ignores_stopped_domains` | PASS |
| Q043 | Bootstrap busy / SSH restart | Read-only probes retry; init script not replayed | `runner/tests/test_qemu_lifecycle_hardening.py::test_cloud_init_readonly_probe_retries_running_and_ssh_restart` | PASS |
| Q044 | Bootstrap never completes | Bounded failure, no script/publication, guest cleaned | `runner/tests/test_qemu_lifecycle_hardening.py::test_cloud_init_wait_is_bounded_and_failed_build_never_runs_script` | PASS |
| Q045 | Domain state scan interrupted | Raise, never silently omit a guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_domain_discovery_failure_is_never_authoritative_absence` | PASS |
| Q046 | Two overlay creators same path | One exclusive publication; winner inode preserved | `runner/tests/test_qemu_lifecycle_hardening.py::test_overlay_publication_never_clobbers_a_racing_creator` | PASS |
| Q047 | Cloud cache publication race | Existing cache inode never replaced | `runner/tests/test_qemu_lifecycle_hardening.py::test_cloud_cache_publication_preserves_concurrently_published_inode` | PASS |
| Q048 | Two simultaneous starts fit only one | One activates; other fails before overcommit | `runner/tests/test_qemu_lifecycle_hardening.py::test_concurrent_starts_atomically_reserve_memory_against_other_guests` | PASS |
| Q049 | Resume paused RAM reservation | Existing active reservation counted once | `runner/tests/test_qemu_lifecycle_hardening.py::test_paused_resume_does_not_reserve_existing_guest_ram_twice` | PASS |
| Q050 | Slow durable delete plus cancellation | Deletion drains; event loop stays responsive | `runner/tests/test_qemu_lifecycle_hardening.py::test_slow_image_delete_fsync_is_drained_without_blocking_event_loop` | PASS |
| Q051 | Capture / legacy disk metadata | Actual virtual disk floor propagated | `runner/tests/test_qemu_lifecycle_hardening.py::test_capture_and_legacy_inventory_report_actual_virtual_disk_floor` | PASS |
| Q052 | Temporary RAM shortage | Queues, reports reason, no guest until released | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_waits_for_memory_then_runs_without_allocating_while_queued` | PASS |
| Q053 | Cancel memory waiter | Cancelled waiter allocates no guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_queue_cancel_removes_waiter_without_guest` | PASS |
| Q054 | Cancel concurrency-slot waiter | Only active build executes | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_slot_wait_cancel_never_starts_second_guest` | PASS |
| Q055 | Memory never becomes available | Bounded known failure, no guest/disk/network | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_queue_timeout_is_known_failure_without_allocation` | PASS |
| Q056 | Configured build RAM exceeds physical budget | Immediate clear failure, no endless queue | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_impossible_memory_fails_immediately` | PASS |
| Q057 | Host has fewer CPUs than configured parallel builds | Actual concurrency capped; queued jobs later run | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_cpu_budget_limits_parallelism` | PASS |
| Q058 | Build vCPUs exceeds physical logical CPUs | Immediate clear failure, no guest allocation | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_impossible_cpu_fails_immediately` | PASS |
| Q059 | Memory consumed between queue probe and create | Retry only typed temporary capacity failure | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_rechecks_memory_after_capacity_race` | PASS |
| Q060 | Libvirt inspection or allocation error | Single failure, no unsafe execution replay | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_does_not_retry_unknown_libvirt_failure` | PASS |
| Q061 | Build interrupted / operation identity | Exact deterministic guest ID supports recovery | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_guest_identity_matches_operation` | PASS |
| Q062 | Malformed operation identity | Reject before cache/network/disk work | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_invalid_operation_identity_has_no_effects` | PASS |
| Q063 | Progress delivery fails before admission | No guest mutation; clear failure | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_queue_progress_failure_does_not_allocate` | PASS |
| Q064 | Cancel after guest activated | Drain issued effects/cleanup before releasing slot | `runner/tests/test_qemu_lifecycle_hardening.py::test_build_cancel_during_effect_drains_and_releases_slot` | PASS |
| Q065 | Remove guest state=1, persistent=True | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[1-True]` | PASS |
| Q066 | Remove guest state=3, persistent=True | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[3-True]` | PASS |
| Q067 | Remove guest state=3, persistent=False | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[3-False]` | PASS |
| Q068 | Remove guest state=5, persistent=True | Domain gone before disk/seed/key/network cleanup | `runner/tests/test_storage_inventory.py::test_workspace_remove_confirms_domain_gone_before_cleanup[5-True]` | PASS |
| Q069 | Remove failure: destroy | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[destroy]` | PASS |
| Q070 | Remove failure: undefine | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[undefine]` | PASS |
| Q071 | Remove failure: present | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[present]` | PASS |
| Q072 | Remove failure: enumeration | Retain recovery disk/network/seed/key | `runner/tests/test_storage_inventory.py::test_unverified_domain_removal_retains_network_disk_seed_and_key[enumeration]` | PASS |
| Q073 | Remove state=1, blocker=foreign | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[1-foreign]` | PASS |
| Q074 | Remove state=1, blocker=overlay | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[1-overlay]` | PASS |
| Q075 | Remove state=1, blocker=partial | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[1-partial]` | PASS |
| Q076 | Remove state=3, blocker=foreign | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[3-foreign]` | PASS |
| Q077 | Remove state=3, blocker=overlay | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[3-overlay]` | PASS |
| Q078 | Remove state=3, blocker=partial | Reject before destroy; preserve guest | `runner/tests/test_storage_inventory.py::test_workspace_remove_checks_graph_before_destroy[3-partial]` | PASS |
| Q079 | Capture source running / unscrubbed / ready | Only stopped scrubbed source publishes standalone exclusive artifact | `runner/tests/test_storage_inventory.py::test_capture_self_contained_stopped_scrubbed_and_exclusive` | PASS |
| Q080 | Image has descendant or unreadable graph | Deletion blocked safely | `runner/tests/test_storage_inventory.py::test_capture_descendant_and_unreadable_scan_block_delete` | PASS |
| Q081 | Crash / ENOSPC / invalid publication target | No false-ready marker or arbitrary path write | `runner/tests/test_storage_inventory.py::test_publication_crash_marker_enospc_and_invalid_path` | PASS |
| Q082 | Physical remove error | Disk retained for retry | `runner/tests/test_storage_inventory.py::test_failed_remove_retains_disk` | PASS |
| Q083 | Cancellation while threaded effect outstanding | Mutation lock retained until effect settles | `runner/tests/test_storage_inventory.py::test_mutation_cancellation_drains_thread_and_holds_lock` | PASS |
| Q084 | Published artifact plus lost acknowledgement | Recover exact publication; no rebuild | `runner/tests/test_storage_inventory.py::test_journal_recovers_exact_publication_without_rerun` | PASS |
| Q085 | Partial create removal | Only exact owned partial resources removed | `runner/tests/test_storage_inventory.py::test_explicit_partial_create_removal_preserves_unrelated_disk` | PASS |
| Q086 | Graph changes while removing domain | Recheck dependencies before unlink | `runner/tests/test_storage_inventory.py::test_workspace_delete_rechecks_after_undefine` | PASS |
| Q087 | Corrupt output versus changed source | Source proof retained only for unchanged source | `runner/tests/test_storage_inventory.py::test_source_scrub_survives_corrupt_output_but_not_external_disk_write` | PASS |
| Q088 | Image conversion truncated | No usable publication; source evidence remains | `runner/tests/test_storage_inventory.py::test_truncated_conversion_failure_retains_safe_source_evidence` | PASS |
| Q089 | Same-size/mtime external artifact mutation | Hash cache invalidates using inode metadata | `runner/tests/test_storage_inventory.py::test_publication_hash_cache_invalidates_same_size_and_mtime` | PASS |
| Q090 | Published path replaced externally | Foreign replacement not deleted | `runner/tests/test_storage_inventory.py::test_cached_publication_foreign_overwrite_cannot_be_deleted` | PASS |
| Q091 | Unpublished output exists | Dependency protects parent; output not cloneable | `runner/tests/test_storage_inventory.py::test_unpublished_images_keep_physical_dependencies_but_cannot_be_used` | PASS |
| Q092 | Owned and orphan cloud-init files | Inventory maps exact ownership only | `runner/tests/test_storage_inventory.py::test_cloud_init_inventory_exact_ownership_and_orphans` | PASS |
| Q093 | Delete original captured source | Standalone artifact remains cloneable | `runner/tests/test_storage_inventory.py::test_standalone_capture_survives_origin_disk_and_seed_removal` | PASS |
| Q094 | Two parallel physical QCOW2/ISO builds | Distinct guests/subnets, complete inventory, clean teardown | `runner/tests/test_parallel_image_builds.py::test_parallel_builds_have_distinct_networks_and_inventory_stays_available` | PASS |
| Q095 | One parallel build init fails | Other succeeds; both guests cleaned | `runner/tests/test_parallel_image_builds.py::test_one_failed_build_is_cleaned_without_aborting_another` | PASS |
| Q096 | Cancel running build with inventory probe | Build drains cleanup; unrelated inventory available | `runner/tests/test_parallel_image_builds.py::test_caller_cancellation_drains_build_and_cleanup_without_global_lock` | PASS |

Pre-execution addendum after settings audit: invalid reconfiguration must preserve a running guest. These expectations were added before executing these tests.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q097 | Running guest requests impossible RAM | Reject before shutdown; guest and definition unchanged | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_reconfigure_preserves_running_guest[memory]` | PASS |
| Q098 | Running guest requests zero CPUs | Reject before shutdown; guest and definition unchanged | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_reconfigure_preserves_running_guest[cpu]` | PASS |
| Q099 | Running guest requests disk shrink | Reject before shutdown; guest and disk unchanged | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_reconfigure_preserves_running_guest[disk]` | PASS |
| Q100 | Running guest disk inspection fails | Reject before shutdown; preserve running guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_reconfigure_preserves_running_guest[disk-inspection]` | PASS |
| Q101 | Resource restart gap plus queued build | Target RAM remains reserved while stopped; build waits then proceeds | `runner/tests/test_qemu_lifecycle_hardening.py::test_reconfigure_reserves_ram_during_stop_start_gap` | PASS |
| Q102 | Resource change fails after admission | Release transient RAM reservation on every error | `runner/tests/test_qemu_lifecycle_hardening.py::test_reconfigure_failure_releases_transient_reservation` | PASS |

Pre-execution hypervisor state addendum: resource-blocked guests remain running, power-suspended guests wake, graceful shutdown already in progress is awaited.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q103 | libvirt nostate + start | Reject without mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[nostate]` | PASS |
| Q104 | libvirt running + start | Idempotent, no new activation | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[running]` | PASS |
| Q105 | libvirt blocked + start | Idempotent, no new activation | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[blocked]` | PASS |
| Q106 | libvirt paused + start | Resume existing guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[paused]` | PASS |
| Q107 | libvirt shutdown + start | Reject without mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[shutdown]` | PASS |
| Q108 | libvirt shutoff + start | Create existing defined guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[shutoff]` | PASS |
| Q109 | libvirt crashed + start | Reject without mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[crashed]` | PASS |
| Q110 | libvirt suspended + start | Wake existing guest | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_start[suspended]` | PASS |
| Q111 | libvirt nostate + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[nostate]` | PASS |
| Q112 | libvirt running + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[running]` | PASS |
| Q113 | libvirt blocked + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[blocked]` | PASS |
| Q114 | libvirt paused + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[paused]` | PASS |
| Q115 | libvirt shutdown + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[shutdown]` | PASS |
| Q116 | libvirt shutoff + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[shutoff]` | PASS |
| Q117 | libvirt crashed + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[crashed]` | PASS |
| Q118 | libvirt suspended + stop | Verified shutoff; no duplicate shutdown while already shutting down | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_stop[suspended]` | PASS |
| Q119 | libvirt nostate + status | unknown | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[nostate]` | PASS |
| Q120 | libvirt running + status | running | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[running]` | PASS |
| Q121 | libvirt blocked + status | running | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[blocked]` | PASS |
| Q122 | libvirt paused + status | paused | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[paused]` | PASS |
| Q123 | libvirt shutdown + status | stopping | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[shutdown]` | PASS |
| Q124 | libvirt shutoff + status | stopped | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[shutoff]` | PASS |
| Q125 | libvirt crashed + status | failed | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[crashed]` | PASS |
| Q126 | libvirt suspended + status | suspended | `runner/tests/test_qemu_lifecycle_hardening.py::test_hypervisor_state_status[suspended]` | PASS |

Pre-execution interrupted-guest addendum: killing the runner does not stop commands already running inside a guest. Recovery must prove exact guest identity and shutoff before releasing an interrupted operation.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q127 | Interrupted running owned guest | Shut off exact guest; return stopped without credential-clean proof | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_stops_exact_owned_guest[running]` | PASS |
| Q128 | Interrupted paused owned guest | Shut off exact guest; paused state not treated absent | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_stops_exact_owned_guest[paused]` | PASS |
| Q129 | Interrupted guest metadata has wrong owner | No guest shutdown or disk mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_rejects_unverified_identity[metadata]` | PASS |
| Q130 | Interrupted guest points at foreign disk | No guest shutdown or disk mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_rejects_unverified_identity[disk]` | PASS |
| Q131 | Guest incarnation replaced during shutdown | Do not claim quiescence for replacement | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_rejects_changed_incarnation` | PASS |
| Q132 | Interrupted guest already completely absent | Exact absence proof returns absent | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_absence_requires_all_resources_gone[absent]` | PASS |
| Q133 | Interrupted domain absent but disk remains | Complete unconsumed partial allocation is quiescent; retain files and do not claim absence | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_absence_requires_all_resources_gone[partial]` | PASS |
| Q134 | Interrupted guest shutdown fails | Error retained, no false stopped/clean proof | `runner/tests/test_qemu_lifecycle_hardening.py::test_quiesce_interrupted_workspace_stop_failure_is_not_quiescence` | PASS |

Continued audit plan (2026-10-31), recorded before execution: Q133 changes because a complete no-domain observation must permit automatic recovery of failed pre-domain creation. Ownership is required separately for later deletion.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q135 | Interrupted partial allocation has a foreign disk consumer | Retain all resources; do not claim quiescence | `runner/tests/test_qemu_lifecycle_hardening.py::test_partial_interruption_rejects_foreign_consumer` | PASS |
| Q136 | Interrupted partial allocation cannot be fully inspected | Retain all resources; retry recovery later | `runner/tests/test_qemu_lifecycle_hardening.py::test_partial_interruption_rejects_incomplete_inventory` | PASS |
| Q137 | create resource request cpu-zero | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[create-cpu-zero]` | PASS |
| Q138 | create resource request cpu-negative | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[create-cpu-negative]` | PASS |
| Q139 | create resource request cpu-capacity | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[create-cpu-capacity]` | PASS |
| Q140 | create resource request memory-zero | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[create-memory-zero]` | PASS |
| Q141 | create resource request memory-negative | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[create-memory-negative]` | PASS |
| Q142 | create resource request disk-zero | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[create-disk-zero]` | PASS |
| Q143 | create resource request disk-negative | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[create-disk-negative]` | PASS |
| Q144 | clone resource request cpu-zero | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[clone-cpu-zero]` | PASS |
| Q145 | clone resource request cpu-negative | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[clone-cpu-negative]` | PASS |
| Q146 | clone resource request cpu-capacity | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[clone-cpu-capacity]` | PASS |
| Q147 | clone resource request memory-zero | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[clone-memory-zero]` | PASS |
| Q148 | clone resource request memory-negative | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[clone-memory-negative]` | PASS |
| Q149 | clone resource request disk-zero | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[clone-disk-zero]` | PASS |
| Q150 | clone resource request disk-negative | Reject before allocating disk/network/domain; explicit zero never falls back to defaults | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_initial_resources_have_no_effects[clone-disk-negative]` | PASS |

Continued pre-execution integrity addendum: publication readiness and graph observation are separate evidence. A readable image with bad marker stays unknown and unusable; its fully observed dependencies still fence deletion. An unreadable QCOW2/header remains an incomplete graph and blocks mutation.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q151 | Unrelated publication SHA mismatch | Image stays unknown; independent workspace deletion succeeds | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_publication_does_not_block_unrelated_workspace_removal[hash]` | PASS |
| Q152 | Unrelated publication malformed JSON | Image stays unknown; independent workspace deletion succeeds | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_publication_does_not_block_unrelated_workspace_removal[json]` | PASS |
| Q153 | Invalid marker on image with backing disk | Follow backing graph and retain referenced workspace storage | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_publication_still_fences_its_backing_dependencies[hash]` | PASS |
| Q154 | Malformed marker on image with backing disk | Follow backing graph and retain referenced workspace storage | `runner/tests/test_qemu_lifecycle_hardening.py::test_invalid_publication_still_fences_its_backing_dependencies[json]` | PASS |
| Q155 | Published disk header unreadable | Graph remains incomplete; retain unrelated workspace until graph can be inspected | `runner/tests/test_qemu_lifecycle_hardening.py::test_unreadable_published_disk_keeps_dependency_fence` | PASS |

Pre-execution observation-gap plan (continued review): every hypervisor state must agree between direct status and discovered workspace lists. Inventory must preserve nonusable states rather than interpreting every active domain as a usable running guest. These are fault-injected libvirt observations; real paused guests are covered separately by the root VM matrix.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q156 | Listed libvirt guest nostate | Direct/list observation agree on unknown; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[nostate]` | PASS |
| Q157 | Listed libvirt guest running | Direct/list observation agree on running; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[running]` | PASS |
| Q158 | Listed libvirt guest blocked | Direct/list observation agree on running; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[blocked]` | PASS |
| Q159 | Listed libvirt guest paused | Direct/list observation agree on paused; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[paused]` | PASS |
| Q160 | Listed libvirt guest shutdown | Direct/list observation agree on stopping; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[shutdown]` | PASS |
| Q161 | Listed libvirt guest shutoff | Direct/list observation agree on stopped; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[shutoff]` | PASS |
| Q162 | Listed libvirt guest crashed | Direct/list observation agree on failed; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[crashed]` | PASS |
| Q163 | Listed libvirt guest suspended | Direct/list observation agree on suspended; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[suspended]` | PASS |
| Q164 | Listed libvirt guest unrecognized | Direct/list observation agree on unknown; no mutation | `runner/tests/test_qemu_lifecycle_hardening.py::test_listed_hypervisor_state_matches_direct_status[unrecognized]` | PASS |
| Q165 | Inventory libvirt guest nostate | Observe unknown; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[nostate]` | PASS |
| Q166 | Inventory libvirt guest running | Observe running; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[running]` | PASS |
| Q167 | Inventory libvirt guest blocked | Observe running; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[blocked]` | PASS |
| Q168 | Inventory libvirt guest paused | Observe paused; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[paused]` | PASS |
| Q169 | Inventory libvirt guest shutdown | Observe stopping; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[shutdown]` | PASS |
| Q170 | Inventory libvirt guest shutoff | Observe exited; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[shutoff]` | PASS |
| Q171 | Inventory libvirt guest crashed | Observe failed; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[crashed]` | PASS |
| Q172 | Inventory libvirt guest suspended | Observe suspended; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[suspended]` | PASS |
| Q173 | Inventory libvirt guest unrecognized | Observe unknown; only running/blocked may imply usable running state | `runner/tests/test_qemu_lifecycle_hardening.py::test_inventory_hypervisor_state_is_not_inferred_from_activity[unrecognized]` | PASS |

Pre-execution fresh-runner addendum: an unused configured cloud-image cache is still an owned inventory root. Fresh startup must create it before the first inventory or image resolution; an unwritable root must fail startup with a concrete configuration diagnostic.

| ID | Situation | Expected behavior | Exact pytest node | Result |
| --- | --- | --- | --- | --- |
| Q174 | Brand-new disk/snapshot/cache directories before any build or base resolution | Initialize all configured roots, report complete empty inventory, and require no base download or real domain | `runner/tests/test_qemu_lifecycle_hardening.py::test_fresh_runtime_initializes_cache_before_first_inventory` | PASS |
| Q175 | Configured fresh image-cache directory cannot be created | Fail startup with the directory permission/fix diagnostic; never publish an apparently healthy incomplete runtime | `runner/tests/test_qemu_lifecycle_hardening.py::test_fresh_runtime_cache_permission_failure_is_explicit` | PASS |
