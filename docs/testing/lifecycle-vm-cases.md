# Real VM and user-state verification plan

Recorded before execution. `V001`–`V032` use Ubuntu 22.04; `V033`–`V064`
repeat the same scenarios using Ubuntu 24.04. All use real KVM/libvirt, QCOW2,
SSH and cloud-init. Network/storage fault injection is labelled explicitly;
it does not claim to reproduce a physical SSD controller failure.

| IDs (22.04 / 24.04) | Scenario → expected outcome | Status |
|---|---|---|
| V001 / V033 | Two distinct image builds → overlapping initialization if admitted by host RAM/CPU. | PASS — both releases |
| V002 / V034 | Third build at concurrency limit → queued; no temporary guest allocated yet. | PASS — both releases |
| V003 / V035 | Workspace create while builds initialize → independently completes with correct image. | PASS — both releases |
| V004 / V036 | Workspace CPU/RAM/disk update while builds run → completes; data preserved. | PASS — both releases |
| V005 / V037 | Workspace stop during builds → stops; builders continue. | PASS — both releases |
| V006 / V038 | Queued build after a slot frees → automatically starts; all three build results correct. | PASS — both releases |
| V007 / V039 | Published definition images → standalone QCOW2, valid hash/identity/minimum disk manifest. | PASS — both releases |
| V008 / V040 | Inventory after builds → complete, published images present, temporary builders absent. | PASS — both releases |
| V009 / V041 | Definition-backed workspace → correct recipe marker; cloud-init complete. | PASS — both releases |
| V010 / V042 | Backend unavailable during create → VM completes; terminal result retained durably. | PASS — both releases |
| V011 / V043 | Reconnect, result replay and ACK → same operation; no pending result after matching ACK. | PASS — both releases |
| V012 / V044 | Duplicate durable/raw create → existing guest and disk inode/data preserved. | PASS — both releases |
| V013 / V045 | Controlled stop after injected test credential → stopped; credential scrub proven. | PASS — both releases |
| V014 / V046 | Capture stopped clean guest → standalone image with source identity and disk floor. | PASS — both releases |
| V015 / V047 | Clone capture and resume source concurrently → both running; persisted marker preserved. | PASS — both releases |
| V016 / V048 | Clone below image disk floor → rejected before domain/network/disk allocation. | PASS — both releases |
| V017 / V049 | Delete image with live clone dependency → refused; clone data readable. | PASS — both releases |
| V018 / V050 | Tampered publication hash → clone refused; no guest allocation. | PASS — both releases |
| V019 / V051 | Live CPU/RAM/disk growth → XML/settings reflect request; persisted file survives. | PASS — both releases |
| V020 / V052 | Disk shrink request → never shrinks existing disk or loses data. | PASS — both releases |
| V021 / V053 | Impossible RAM request → clear failure before restart/storage mutation; guest remains usable. | PASS — both releases |
| V022 / V054 | Same-guest stop/resume submitted concurrently → serialized completion; final running state. | PASS — both releases |
| V023 / V055 | Externally paused guest stop → actual shutoff, no false scrub proof. | PASS — both releases |
| V024 / V056 | Resume stopped guest → readable persisted data; controlled stop restores scrub proof. | PASS — both releases |
| V025 / V057 | Delete capture origin → captured clone remains usable and independent. | PASS — both releases |
| V026 / V058 | Recreate physically removed ID → new incarnation; no stale storage reuse. | PASS — both releases |
| V027 / V059 | Repeated exact removal → absent domain/network/disk/seed/host-key; idempotent success. | PASS — both releases |
| V028 / V060 | Remove last clone then image → succeeds; no physical image dependency remains. | PASS — both releases |
| V029 / V061 | Init script exits nonzero → failed build; no published image; temporary guest cleaned. | PASS — both releases |
| V030 / V062 | Build after failed build → queue slot released; next image publishes successfully. | PASS — both releases |
| V031 / V063 | Runner SIGKILL during workspace initialization → restart seals failure; initialization never replayed. | PASS — both releases |
| V032 / V064 | Recovered partial workspace → exact cleanup succeeds without admin acknowledgement. | PASS — both releases |

Additional final-code scenarios, each with its own evidence:

| ID | Scenario → expected outcome | Verification | Status |
|---|---|---|---|
| V065 | Inject temporary host RAM pressure with real VM build → queue without allocation; automatically build when pressure removed. | VM harness fault mode | PASS — hardware-faults/report.json |
| V066 | Runner SIGKILL during real image initialization → exact orphan builder stopped/removed; failed retryable operation; recipe not replayed. | VM harness crash-build mode | PASS — crash-recovery/report.json |
| V067 | Real guest disk write throttle → slow write completes; event loop and unrelated guest operation responsive. | libvirt blockIoTune + SSH | PASS — hardware-faults/report.json |
| V068 | Guest abruptly powered off → actual stopped state discovered; resume and controlled stop possible. | libvirt destroy + production runtime | PASS — hardware-faults/report.json |
| V069 | Real publication, result not ACKed, runner restarted → successful exact result recovered without rebuild. | Real QCOW2 + SQLite journal restart | PASS — crash-recovery/report.json |
| V070 | Real loopback backend stopped during lifecycle → completes offline; reconnect/ACK without duplicate execution. | `test_lifecycle_transport_integration.py` | PASS — runner/full-verified.xml |
| V071 | Tiny real filesystem filled → failed QCOW2 write does not replace existing publication. | isolated small filesystem fault | PASS — hardware-faults/report.json |
| V072 | Libvirt inventory unavailable → incomplete observation never proves absence or allows destructive deletion. | QEMU inventory fault regression | PASS — hardware-faults/report.json |
| V073 | Runner SIGKILL after disk allocation, before domain definition → automatic quiescence preserves partial data; ordinary exact removal succeeds using durable allocation ownership. | Real QCOW2, separate runner process, journal restart | PASS — crash-recovery/report.json |
| V074 | Full Ubuntu 22.04 standard desktop recipe → real build, verified publication, native dependencies, Xfce/KasmVNC, Chrome rendering and stop/start persistence succeed. | Real KVM desktop guest; screenshot and recipe SHA-256 | PASS — desktop22/report.json |
| V075 | Full Ubuntu 24.04 standard desktop recipe → same checks on the other supported Ubuntu release. | Real KVM desktop guest; screenshot and recipe SHA-256 | PASS — desktop24/report.json |
| U001 | Workspace list and deletion failures → clear status, deleted entries hidden. | WorkspacesView tests; [headed browser deleted-row assertion](lifecycle-evidence/frontend/browser-smoke.json) | PASS — frontend/full-suite-green.xml + build-verified.log |
| U002 | Stop/start/capture/reset action combinations → conflict/offline/recovery reservations disable unsafe actions. | WorkspaceActions tests | PASS — frontend/full-suite-green.xml + build-verified.log |
| U003 | Workspace operation events → pending operations clear only on matching terminal state. | workspace store tests | PASS — frontend/full-suite-green.xml + build-verified.log |
| U004 | Chat workspace readiness across statuses → no launch on deleted/offline/blocked workspace. | conversationGroups/readiness tests | PASS — frontend/full-suite-green.xml + build-verified.log |
| U005 | Image definition states → deletion/build conflicts and retry actions clear. | imageDefinitionLifecycle tests | PASS — frontend/full-suite-green.xml + build-verified.log |
| U006 | Interrupted operation inspector → exact evidence and explicit permitted actions only. | RunnerOperationsPanel tests | PASS — frontend/full-suite-green.xml + build-verified.log |
| U007 | Workspace configuration/reset → frontend readiness/version and resource bounds validated; authoritative image disk floor enforced by backend before allocation. | WorkspaceRecreateDialog + EditWorkspaceDialog; backend disk-floor tests + V016/V048 | PASS — frontend/full-suite-green.xml + build-verified.log |
| U008 | Frontend after removal of duplicate deleted state → type check succeeds. | webapp type-check | PASS — frontend/full-suite-green.xml + build-verified.log |

Physical host power loss, defective RAM and SSD firmware/controller failure cannot
be safely induced in this shared VM. Software boundaries are exercised with
process kills, delayed IO, full filesystems, inspection failures and transport
outages. Such injected tests are never labelled physical hardware tests.

U007 execution clarification: frontend image metadata does not expose the image-specific disk floor. The UI readiness/resource-bound checks and authoritative backend/real-VM floor checks are separate evidence; no client-local image-floor validation is claimed. The original frozen expectation remains in the plan archive.

V067 protocol clarification, recorded before the revised execution: a complete
inventory hashes publication bytes and has no five-second SLA. Start it in
parallel with the throttled write, require another guest's SSH and paused-stop
to complete within five seconds while that write remains unfinished, and verify
event-loop scheduling gaps stay below one second through scan completion.
The inventory must still finish completely and the actual write must last at
least four seconds. Record scan, write and unrelated-operation times separately.
This proves responsive non-storage work; storage graph mutations can wait for
the shared scan lock. Inventory cancellation drains issued IO, so its timeout
does not forcibly terminate an unknown filesystem effect. The original failed
five-second scan attempt is retained as evidence of the stricter harness assumption.
