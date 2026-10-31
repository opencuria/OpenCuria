"""Physical recovery contradictions that require an external ownership repair."""


class RecoveryConflict(RuntimeError):
    """Report contradictory ownership without authorizing resource disposal."""

    code = "publication_ownership_conflict"
