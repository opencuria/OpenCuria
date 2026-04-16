from django.db import migrations


HARDCODED_CHAT_ID = "0af7372f-cb60-4e27-ae07-52937443aeb3"

TARGET_COMMANDS = (
    ("Claude Code", "run", 1),
    ("Claude Code", "run_first", 2),
    ("Codex CLI", "run", 1),
    ("Codex CLI", "run_first", 2),
    ("GitHub Copilot", "run", 1),
    ("GitHub Copilot Student", "run", 1),
)


def _renderable_args(args):
    if not isinstance(args, list):
        return args

    rendered = list(args)
    for index, arg in enumerate(rendered):
        if arg == HARDCODED_CHAT_ID:
            rendered[index] = "{chat_id}"
            continue
        if arg == "--model" and index + 1 < len(rendered):
            model_value = rendered[index + 1]
            if isinstance(model_value, str) and model_value != "{model}":
                rendered[index + 1] = "{model}"
    return rendered


def fix_agent_command_template_defaults(apps, schema_editor):
    agent_command_model = apps.get_model("runners", "AgentCommand")

    for agent_name, phase, order in TARGET_COMMANDS:
        command = (
            agent_command_model.objects.filter(
                agent__organization__isnull=True,
                agent__name=agent_name,
                phase=phase,
                order=order,
            )
            .select_related("agent")
            .first()
        )
        if command is None:
            continue

        updated_args = _renderable_args(command.args)
        if updated_args == command.args:
            continue

        command.args = updated_args
        command.save(update_fields=["args"])


class Migration(migrations.Migration):
    dependencies = [
        ("runners", "0004_alter_task_type"),
    ]

    operations = [
        migrations.RunPython(
            fix_agent_command_template_defaults,
            migrations.RunPython.noop,
        ),
    ]
