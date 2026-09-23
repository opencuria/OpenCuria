"""Session subpackage: terminals, background, streams, desktop, xdotool.

Session-scoped state owned by ``WorkspaceService`` moves here in Step 5,
one module per session kind. Each module keeps its own keyed lock map
with never-evict semantics (pinned by
``tests/test_services_contract.py``).
"""
