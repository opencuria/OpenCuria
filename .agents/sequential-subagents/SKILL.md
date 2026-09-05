---
name: sequential-subagents
description: Delegates implementation to Task subagents that must run one after another, never in parallel. Use when manually selected.
disable-model-invocation: true
---

# Sequential Subagents

**Implementierung nur durch Subagents** (Task-Tool). Der Parent schreibt keinen Feature-Code selbst.

Erlaubte Subagent-Modelle: Composer 2.5 (`composer-2.5`, NICHT fast!), Grok 4.6 (`cursor-grok-4.6-high`, NICHT fast!), oder Auto (kein `model`-Parameter). Falls ein Plan erstellt wird, muss dieser die erlaubten Subagent-Modelle enthalten.

**Nacheinander, nicht parallel:** Jeden Subagent erst starten, wenn der vorherige fertig ist.

**Parent bleibt verantwortlich:** Plan, Auftrag, Review. Nach jedem Subagent gegenprüfen, ob die Umsetzung den Anforderungen entspricht — bei Abweichungen nachsteuern oder erneut delegieren.

**Abschluss: Plan-Check-Subagent:** Wenn die Implementierung fertig ist, einen weiteren Subagent starten. Auftrag: die Umsetzung **gegen den Plan** prüfen. Dieser Subagent **ändert nichts selbst** (kein Feature-Code, keine Datei-Edits). Dieser Plan-Check-Subagent nutzt Grok 4.6 (`cursor-grok-4.6-high`, NICHT fast!).

Findet er Abweichungen oder Lücken, löst er sie **nicht im Parent**, sondern **selbst** über eigene Sequential-Subagents: denselben Skill `sequential-subagents` anwenden (nacheinander, erlaubte Modelle, Parent schreibt keinen Feature-Code). Erst wenn der Check ohne offene Abweichungen durch ist, ist die Sequenz abgeschlossen.
