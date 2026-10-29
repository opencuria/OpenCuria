# Workspace editor

The webapp uses Monaco, VS Code's standalone editor, in place of the former
read-only file and diff viewers. Opening a file replaces the workspace header
with its tab strip and uses the full available content height. The chat remains
mounted. Reload, download and save actions sit in the tab strip. The chat button
overlays the right tool panel with the existing chat so the file stays visible.
Closing chat restores the previous tool-panel visibility and selected tab;
closing the editor returns chat to the main area. Chat is only docked while an
editor is open. Below the desktop breakpoint, editor and chat stack vertically
so both remain visible.

Files and working-tree diffs share one buffer. Switching tabs or workspaces
retains unsaved edits, undo history, selections and scroll positions. The dot
marks unsaved files; closing one offers Save, Don't save and Cancel. Ctrl/Cmd+S
saves, Ctrl/Cmd+W closes the active file, and the browser warns before leaving
with unsaved buffers. Logging out or changing organizations clears the editor
and retained Monaco models.

The Git panel opens working-tree, index and commit comparisons. The plus stages
one change, the arrow reverts it, and the minus unstages an indexed change.
Selecting changed lines enables the corresponding selection actions. Save
unsaved edits before using Git actions. File saves and Git operations refresh
the affected buffers and repository status. Clean active files also reload when
the window regains focus.

## Transport and conflict handling

- `GET /workspaces/{id}/files/content/?path=...` reads complete UTF-8 text.
- `PUT /workspaces/{id}/files/content/` accepts `path`, `content` and
  `expected_revision`, a SHA-256 token from the preceding read or save. A
  missing file has the token `missing`. Saving preserves file mode and line
  endings and replaces the file through a temporary sibling.
- `GET /workspaces/{id}/git/file-content/` selects a repository path, file and
  `revision` (`worktree`, `index`, `head`, a commit hash or its first parent).
  Missing sides of additions, deletions and root commits are empty text.
- Existing `POST /workspaces/{id}/git/operation/` accepts `stage_patch`,
  `unstage_patch` and `discard_patch`. Requests contain `path`, unified hunks
  without file headers, and expected index/worktree tokens. Unstaging also
  requires `expected_head_revision` and `old_path` for the HEAD side.

The backend reuses workspace access and typed Git-operation permissions. The
runner resolves and checks paths inside the workspace, supplies trusted patch
headers and rejects stale versions with HTTP 409 `stale_document`. The editor
keeps unsaved content visible after save failures and offers explicit reload.

Editable text is limited to 5 MiB. Larger files and binary content retain a
read-only preview/download flow; images and PDFs retain their previews. SVG
opens as editable source. Monaco and its workers are bundled locally and loaded
when a text file opens. Colors follow OpenCuria's light and dark themes.

## Verification

```bash
npm run type-check -w webapp
npm test -w webapp -- --maxWorkers=1
npm run build-only -w webapp
cd runner && .venv/bin/python -m pytest -q tests/test_editor_operations.py
cd ../backend && .venv/bin/python -m pytest -q apps/runners/tests/test_editor_api.py
```
