"""Validated recording references and bounded subagent output."""


def _video_markdown(recording_path: str) -> str:
    return f"\n\n![Computer use]({recording_path})"


#: Canonical workspace-internal computer-use recording directory.
_RECORDING_BASE = "/workspace/.opencuria/computeruse"

#: Safe runner run ids (mirrors ``services/sessions/desktop.py::_RUN_ID_RE``).
_SAFE_RUN_ID_PATTERN = None  # compiled lazily to keep import cost low


def _safe_run_id_re():  # type: ignore[no-untyped-def]
    import re

    global _SAFE_RUN_ID_PATTERN
    if _SAFE_RUN_ID_PATTERN is None:
        _SAFE_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    return _SAFE_RUN_ID_PATTERN


def is_valid_recording_path(target: object) -> bool:
    """True when *target* is a canonical workspace-internal recording path.

    Only absolute paths under
    ``/workspace/.opencuria/computeruse/<safe run id>/...`` ending in
    ``.mp4`` are trusted (no ``..``/``.``/empty segments, no URLs, no
    other workspace paths, no backslashes/control chars). The runner
    always returns exactly such a path for ``record_start``/``record_stop``.
    """
    import posixpath

    if not isinstance(target, str):
        return False
    candidate = target.strip()
    if not candidate:
        return False
    if "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        return False
    if "\\" in candidate:
        return False
    if "://" in candidate:
        return False
    prefix = _RECORDING_BASE + "/"
    if not candidate.startswith(prefix):
        return False
    if not candidate.lower().endswith(".mp4"):
        return False
    rest = candidate[len(prefix) :]
    if "/" not in rest:
        return False
    run_id, remainder = rest.split("/", 1)
    if not run_id or _safe_run_id_re().match(run_id) is None:
        return False
    if not remainder:
        return False
    segments = remainder.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return False
    if posixpath.normpath(candidate) != candidate:
        return False
    return True


def append_video_to_output(output: str, recording_path: str) -> str:
    """Append the recording markdown embed when not already present."""
    marker = f"![Computer use]({recording_path})"
    if marker in (output or ""):
        return output
    return (output or "").rstrip() + _video_markdown(recording_path)


def truncate_task_output(
    output: str,
    recording_path: str | None,
    max_chars: int,
) -> tuple[str, bool]:
    """Truncate *output* for parent task cards, preserving a real video line.

    A video embed is only ever preserved/added when *recording_path* is a
    validated canonical recording path (see :func:`is_valid_recording_path`)
    that is actually present in *output* (a real, previously recorded
    session). When no recording exists (``recording_path`` is ``None``/
    empty/invalid or no marker is present), the output is truncated as
    plain text — never invented via a default path.
    """
    if recording_path and not is_valid_recording_path(recording_path):
        recording_path = None
    if recording_path:
        wanted = f"![Computer use]({recording_path})"
        if wanted not in (output or ""):
            # Preserve only a recording actually embedded in this output.
            recording_path = None
    if not recording_path:
        text = output or ""
        if len(text) <= max_chars:
            return text, False
        notice_reserve = 40
        avail = max(0, max_chars - notice_reserve)
        return text[:avail] + f"\n…[truncated {len(text)} chars total]", True
    full = append_video_to_output(output, recording_path)
    suffix = _video_markdown(recording_path)
    if full.endswith(suffix):
        text = full[: -len(suffix)].rstrip()
    else:
        marker = f"![Computer use]({recording_path})"
        idx = full.rfind(marker)
        if idx == -1:
            return full, False
        text = full[:idx].rstrip()
        suffix = full[idx:]
    if len(text) + len(suffix) <= max_chars:
        return text + suffix, False
    notice_reserve = 40
    avail = max(0, max_chars - len(suffix) - notice_reserve)
    return text[:avail] + f"\n…[truncated {len(text)} chars total]" + suffix, True


def extract_recording_path(output: str) -> str | None:
    """Return the first validated canonical recording path in *output*.

    Only absolute paths under
    ``/workspace/.opencuria/computeruse/<safe run id>/...`` ending in
    ``.mp4`` are trusted; anything else (URLs, ``/etc``, other workspace
    paths, ``..``, wrong extensions) returns ``None`` so callers never
    preserve or forward an attacker-controlled marker.
    """
    import re

    match = re.search(r"!\[Computer use\]\(([^)]+)\)", output or "")
    if match is None:
        return None
    target = match.group(1).strip()
    if not target or not is_valid_recording_path(target):
        return None
    return target
