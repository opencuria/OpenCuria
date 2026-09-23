"""Fine-grained service modules extracted from ``src.service``.

Final architecture (implemented): the ``WorkspaceService`` god class
(7283 lines) has been migrated into this package, one module per bounded
context/responsibility — never split merely for size. ``src.service``
remains as a thin composition root (wiring + delegation + re-exports)
by design: deleting it would force ``main.py``/``websocket.py``/tests
to hand-wire 12 managers (higher coupling, KISS violation). The god
class is gone — every method body lives in its responsibility module.

Module map (each = a meaningful responsibility boundary):

- :mod:`src.services.exec_kernel` — exec kernel: ``WorkspaceContext``
  (single lookup+guard), ``KeyedLockMap`` (never-evict lock factory),
  ``ExecKernel`` (wrap + normalise + runtime dispatch), shared
  path/workdir/filename sanitizers.
- :mod:`src.services.workspace_registry` — cache ownership: ``_cache``,
  runtime resolution, statuses, heartbeat payload, desktop-session
  recovery, VM metrics. Cross-cluster reads via hooks.
- :mod:`src.services.workspace_lifecycle` — sole cross-cluster
  orchestrator: create / stop / resume / remove / cleanup / update +
  health loop. Only importer of sibling managers.
- :mod:`src.services.credentials` — persistent credential inject/remove
  + tar builders + shell helper functions.
- :mod:`src.services.files` — file list/find/read/upload/download/stat/
  write + tar/archive conversion + find builders + size caps.
- :mod:`src.services.harness_exec` — harness command exec (wait + stream)
  + tagged-output parsing.
- :mod:`src.services.sessions.terminals` — interactive PTY sessions.
- :mod:`src.services.sessions.background` — detached background processes
  + verify/reattach + graceful stop.
- :mod:`src.services.sessions.streams` — generic bidirectional streams
  (process + workspace-local TCP relay) + validators.
- :mod:`src.services.sessions.desktop` — desktop lifecycle: Xvnc leases,
  per-action dispatcher, clipboard, geometry, xauthority, recordings.
- :mod:`src.services.sessions.xdotool` — pure xdotool key/combo builders
  + failure markers (Ubuntu 22.04 compat).
- :mod:`src.services.git_service` — full git orchestration (locks,
  exec+timeout, resolve/verify, snapshot/history/diff, 15 mutations,
  dispatch). ``src.git`` stays the pure validation/parsing/env lib.
- :mod:`src.services.images` — image build + artifact CRUD + delete
  reference + create-from-artifact (isolates the docker-SDK import).

Dependency rules: managers depend only on ``exec_kernel`` + registry
lookups + ``runtime/base`` + ``models``; never on each other.
``workspace_lifecycle`` is the sole orchestrator. ``websocket.py`` and
``main.py`` use managers via the ``WorkspaceService`` composition root.
"""
