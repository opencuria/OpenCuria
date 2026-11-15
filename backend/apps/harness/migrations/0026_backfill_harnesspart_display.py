"""Backfill bounded timeline projections in restart-safe committed batches."""

from __future__ import annotations

import io
import json
import re

from django.db import migrations, transaction
from django.db.models import Case, F, Func, JSONField, TextField, Value, When
from django.db.models.functions import Cast, Length

# Keep projection work small even for unusually large part data. Full source
# fields remain in their part rows and are available from the detail endpoint.
MAX_PROJECTION_OUTPUT_CHARS = 512_000
MAX_PROJECTION_JSON_CHARS = 65_536
BATCH_SIZE = 10
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class _RemovePatchContent(Func):
    """Remove duplicated patch file bodies using the database JSON primitive."""

    output_field = JSONField()

    def as_sqlite(self, compiler, connection, **extra_context):
        return super().as_sql(
            compiler,
            connection,
            function="json_remove",
            template="%(function)s(%(expressions)s, '$.old_content', '$.new_content')",
            **extra_context,
        )

    def as_postgresql(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return f"({sql} - 'old_content' - 'new_content')", params


def _short(value, limit=240):
    return " ".join(str(value or "").split())[:limit]


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return result if isinstance(result, dict) else {}
    return {}


def _patch_preview_v1(output):
    """Frozen counterpart of fileDiff.parseFileDiff/collapsedWindow (v1)."""

    def parsed_lines():
        old_no = new_no = 1
        in_hunk = False
        for raw in io.StringIO(output or ""):
            raw = raw.rstrip("\r\n")
            if raw.startswith(("--- ", "+++ ", "\\")):
                continue
            match = _HUNK_RE.match(raw)
            if match:
                old_no, new_no = int(match.group(1)), int(match.group(2))
                in_hunk = True
                continue
            if not raw or raw[0] not in "+- ":
                continue
            if not in_hunk:
                old_no = new_no = 1
                in_hunk = True
            if raw[0] == "+":
                yield {
                    "type": "add",
                    "oldNo": None,
                    "newNo": new_no,
                    "content": raw[1:],
                }
                new_no += 1
            elif raw[0] == "-":
                yield {
                    "type": "del",
                    "oldNo": old_no,
                    "newNo": None,
                    "content": raw[1:],
                }
                old_no += 1
            else:
                yield {
                    "type": "context",
                    "oldNo": old_no,
                    "newNo": new_no,
                    "content": raw[1:],
                }
                old_no += 1
                new_no += 1

    additions = deletions = line_count = 0
    first_change = last_change = -1
    for index, line in enumerate(parsed_lines()):
        line_count += 1
        if line["type"] == "add":
            additions += 1
        elif line["type"] == "del":
            deletions += 1
        if line["type"] != "context":
            if first_change < 0:
                first_change = index
            last_change = index

    if line_count <= 4 or first_change < 0:
        start, end = 0, min(line_count, 4)
    elif additions + deletions >= 1 and additions <= 1 and deletions <= 1:
        start = max(0, first_change - 1)
        end = min(line_count, last_change + 2, start + 4)
    else:
        previous = next(
            (
                line["type"]
                for index, line in enumerate(parsed_lines())
                if index == first_change - 1
            ),
            None,
        )
        start = first_change - 1 if previous == "context" else first_change
        end = min(line_count, start + 4)

    preview = []
    for index, line in enumerate(parsed_lines()):
        if start <= index < end:
            line = dict(line)
            line["content"] = line["content"][:500]
            preview.append(line)
    return preview, additions, deletions


def _build_display_v1(part_type, title, call_id, input_data, output, meta):
    """Frozen v1 timeline projection; do not import current runtime code here."""
    args = input_data if isinstance(input_data, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    display = {}
    if part_type == "tool":
        tool = str(args.get("tool") or meta.get("tool") or "")
        if not tool and title:
            tool = str(title).split(" ", 1)[0].strip().lower()
        display.update(tool=tool, summary=_short(title), step=meta.get("step"))
        if tool in {"question", "ask_user"}:
            tool_args = _object(args.get("arguments"))
            questions = meta.get("questions") or tool_args.get("questions")
            result = _object(output)
            answers = meta.get("answers")
            if not isinstance(answers, list):
                answers = result.get("answers", [])
            rows = []
            if isinstance(questions, list):
                for index, item in enumerate(questions[:8]):
                    if not isinstance(item, dict):
                        continue
                    choices = item.get("options", [])
                    options = []
                    if isinstance(choices, list):
                        for choice in choices[:8]:
                            if isinstance(choice, dict):
                                options.append(
                                    {
                                        "label": _short(choice.get("label"), 500),
                                        "description": _short(
                                            choice.get("description"), 500
                                        ),
                                    }
                                )
                    rows.append(
                        {
                            "header": _short(item.get("header"), 500),
                            "question": _short(item.get("question"), 500),
                            "options": options,
                            "multiple": bool(item.get("multiple", False)),
                            "answer": _short(
                                answers[index]
                                if isinstance(answers, list) and index < len(answers)
                                else "",
                                500,
                            ),
                        }
                    )
            ask_question = tool_args.get("question") or meta.get("question")
            if not rows and ask_question:
                answer = meta.get("answer")
                if answer is None:
                    answer = output if not result else ""
                rows.append(
                    {
                        "header": "",
                        "question": _short(ask_question, 500),
                        "options": [],
                        "multiple": False,
                        "answer": _short(answer, 500),
                    }
                )
            display["question_rows"] = rows
            display["questions"] = [row["question"] for row in rows]
            display["answers"] = [row["answer"] for row in rows]
    elif part_type == "reasoning":
        normalized = (output or "").replace("\r\n", "\n").replace("\r", "\n")
        display.update(
            summary=_short(
                normalized.strip().splitlines()[-1] if normalized.strip() else ""
            ),
            _reasoning_line=normalized.split("\n")[-1][:240],
            step=meta.get("step"),
        )
    elif part_type == "patch":
        preview, additions, deletions = _patch_preview_v1(output)
        display.update(
            path=_short(meta.get("path"), 500),
            preview=preview,
            additions=additions,
            deletions=deletions,
            step=meta.get("step"),
            tool=_short(meta.get("tool"), 100),
        )
    elif part_type == "agent":
        agent = meta.get("agent_meta", {})
        safe = {
            key: _short(agent.get(key), 500)
            for key in (
                "verification",
                "analysis",
                "next_action",
                "action",
                "action_kind",
            )
            if isinstance(agent, dict) and agent.get(key)
        }
        display.update(
            summary=(
                safe.get("analysis")
                or safe.get("next_action")
                or _short(title, 240)
            ),
            agent_meta=safe,
            step=meta.get("step"),
        )
    elif part_type in ("step-start", "step-finish"):
        display.update(
            step=meta.get("step"),
            cost=meta.get("cost", 0),
            tokens=meta.get("tokens", {})
            if isinstance(meta.get("tokens", {}), dict)
            else {},
        )
    elif part_type == "subtask":
        display.update(
            summary=_short(title),
            subtask_id=str(meta.get("subtask_id") or ""),
            child_session_id=str(meta.get("child_session_id") or ""),
            agent=str(meta.get("agent") or ""),
            status=str(meta.get("status") or ""),
            step=meta.get("step"),
        )
    elif part_type == "compaction":
        display.update(
            summary=_short(output, 600),
            auto=bool(meta.get("auto", True)),
            overflow=bool(meta.get("overflow", False)),
        )
    elif part_type == "text":
        display["step"] = meta.get("step")
    else:
        display["summary"] = _short(title)
    return display


def backfill_part_display(apps, schema_editor):
    """Backfill in bounded committed batches; safe to rerun after interruption."""
    HarnessPart = apps.get_model("harness", "HarnessPart")
    alias = schema_editor.connection.alias
    last_pk = None
    while True:
        queryset = HarnessPart.objects.using(alias).order_by("pk")
        if last_pk is not None:
            queryset = queryset.filter(pk__gt=last_pk)
        limited_projection = Case(
            When(
                output_length__lte=MAX_PROJECTION_OUTPUT_CHARS,
                then=F("output"),
            ),
            default=Value(""),
            output_field=TextField(),
        )
        limited_json = Case(
            When(
                json_length__lte=MAX_PROJECTION_JSON_CHARS,
                then=F("input"),
            ),
            default=Value({}, output_field=JSONField()),
            output_field=JSONField(),
        )
        limited_meta = Case(
            When(
                json_length__lte=MAX_PROJECTION_JSON_CHARS,
                then=F("meta"),
            ),
            default=Value({}, output_field=JSONField()),
            output_field=JSONField(),
        )
        batch = list(
            queryset.annotate(
                output_length=Length("output"),
                json_length=Length(Cast(F("input"), TextField()))
                + Length(Cast(F("meta"), TextField())),
            ).annotate(
                projection_output=Case(
                    When(type="patch", then=limited_projection),
                    When(type="agent", then=limited_projection),
                    When(type="reasoning", then=limited_projection),
                    When(type="compaction", then=limited_projection),
                    default=Value(""),
                    output_field=TextField(),
                ),
                projection_input=limited_json,
                projection_meta=limited_meta,
            ).values_list(
                "pk",
                "type",
                "title",
                "call_id",
                "projection_input",
                "projection_output",
                "projection_meta",
            )[:BATCH_SIZE]
        )

        if not batch:
            break
        updates = []
        patch_ids = []
        for part_id, part_type, title, call_id, input_data, output, meta in batch:
            safe_meta = dict(meta or {})
            if part_type == "patch":
                safe_meta.pop("old_content", None)
                safe_meta.pop("new_content", None)
                patch_ids.append(part_id)
            updates.append(
                HarnessPart(
                    pk=part_id,
                    display=_build_display_v1(
                        part_type,
                        title or "",
                        call_id or "",
                        input_data or {},
                        output or "",
                        safe_meta,
                    ),
                )
            )
        # atomic=False on the migration ensures this is a real per-batch commit.
        with transaction.atomic(using=alias):
            HarnessPart.objects.using(alias).bulk_update(
                updates, ["display"], batch_size=BATCH_SIZE
            )
            if patch_ids:
                HarnessPart.objects.using(alias).filter(pk__in=patch_ids).update(
                    meta=_RemovePatchContent(F("meta"))
                )
        last_pk = batch[-1][0]


def noop_reverse(apps, schema_editor):
    """Keep derived metadata on reverse; old scrubbed file copies are unrecoverable."""


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("harness", "0025_harnesspart_display")]
    operations = [migrations.RunPython(backfill_part_display, noop_reverse)]
