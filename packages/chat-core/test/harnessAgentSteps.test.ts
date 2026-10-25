import { describe, expect, it } from "vitest";

import type { HarnessMessage, HarnessPart } from "../src/harness";
import {
  agentStepStatus,
  buildAgentStepViews,
  hasRunningAgentSequence,
} from "../src/harnessAgentSteps";
import { applyPartDelta } from "../src/harnessReducer";

function part(
  type: HarnessPart["type"],
  step: unknown,
  state: HarnessPart["state"] = "completed",
): HarnessPart {
  return {
    id: `${type}-${String(step)}`,
    session_id: "session",
    type,
    state,
    title: "",
    output: "",
    meta: { step },
  };
}

describe("Agent-S timeline characterization", () => {
  it.each(["pending", "running", "completed", "error"] as const)(
    "preserves %s plan state with and without valid step metadata",
    (state) => {
      for (const step of [null, undefined, "2", NaN, Infinity, -Infinity, 2]) {
        const plan = part("agent", step, state);
        const [view] = buildAgentStepViews([plan], { streaming: true });
        const running = state === "running" || state === "pending";
        expect(view).toMatchObject({
          part: plan,
          step: step === 2 ? 2 : null,
          status: running
            ? "in_progress"
            : state === "error" && step !== 2
              ? "error"
              : "completed",
          live: running || (state === "error" && step !== 2),
          legacy: true,
        });
      }
    },
  );

  it("preserves marker precedence, duplicate plans and chronological order", () => {
    const plans = [part("agent", 8), part("agent", 2), part("agent", 2)];
    plans[2]!.id = "revised-plan";
    const parts = [
      ...plans,
      part("step-start", 2),
      part("step-finish", 2),
      part("step-start", 2),
      part("tool", 2, "error"),
      part("step-finish", 8),
    ];
    const views = buildAgentStepViews(parts, { streaming: true });
    expect(views.map(({ part: plan }) => plan)).toEqual(plans);
    expect(views.map(({ status }) => status)).toEqual([
      "completed",
      "error",
      "error",
    ]);
    expect(views.map(({ live }) => live)).toEqual([false, false, true]);
    expect(hasRunningAgentSequence(parts)).toBe(false);
  });

  it("requires a marker before a newer plan supersedes a running plan", () => {
    const first = part("agent", -1.5, "running");
    const later = part("agent", 4);
    expect(buildAgentStepViews([first, later])[0]!.status).toBe("in_progress");
    expect(
      buildAgentStepViews([first, later, part("step-start", -1.5)])[0]!.status,
    ).toBe("completed");
    expect(buildAgentStepViews([first, part("step-start", 4)])[0]!.status).toBe(
      "in_progress",
    );
  });

  it("uses display metadata only when meta.step is nullish", () => {
    const plan = { ...part("agent", null), display: { step: 3 } };
    const start = { ...part("step-start", undefined), display: { step: 3 } };
    const finish = { ...part("step-finish", "3"), display: { step: 3 } };
    expect(buildAgentStepViews([plan, start, finish])[0]!.status).toBe(
      "in_progress",
    );
    expect(hasRunningAgentSequence([plan, start, finish])).toBe(true);
    finish.meta = { step: null };
    expect(buildAgentStepViews([plan, start, finish])[0]!.status).toBe(
      "completed",
    );
    expect(hasRunningAgentSequence([plan, start, finish])).toBe(false);
  });

  it("ignores nonfinite sequence steps and does not match NaN markers", () => {
    for (const step of [NaN, Infinity, -Infinity]) {
      const parts = [part("agent", step), part("step-start", step)];
      expect(hasRunningAgentSequence(parts)).toBe(false);
    }
    const plan = part("agent", NaN, "running");
    expect(agentStepStatus(plan, [part("step-finish", NaN)], NaN)).toBe(
      "in_progress",
    );
    expect(
      agentStepStatus(plan, [part("step-finish", Infinity)], Infinity),
    ).toBe("completed");
  });

  it("requires a start, plan or running reasoning, and lets finish win", () => {
    const reasoning = part("reasoning", 0);
    expect(
      hasRunningAgentSequence([reasoning, part("tool", 0, "running")]),
    ).toBe(false);
    reasoning.state = "running";
    expect(hasRunningAgentSequence([reasoning])).toBe(true);
    expect(hasRunningAgentSequence([part("step-finish", 0), reasoning])).toBe(
      false,
    );
    expect(hasRunningAgentSequence([part("agent", 0, "error")])).toBe(true);
  });

  it("recomputes from streamed events, including late failures and final aborts", () => {
    const messages: HarnessMessage[] = [];
    const stream = (
      id: string,
      delta: Parameters<typeof applyPartDelta>[2],
      step = 1,
    ) =>
      applyPartDelta(messages, "session", delta, {
        messageId: "message",
        partId: id,
        step,
      });
    const message = stream("start", { step_start: 1 });
    stream("plan", { agent: "Plan", agent_meta: { action: "click Next" } });
    expect(
      buildAgentStepViews(message.parts, { streaming: true })[0],
    ).toMatchObject({
      status: "in_progress",
      live: true,
    });
    stream("finish", { step_finish: 1 });
    expect(hasRunningAgentSequence(message.parts)).toBe(false);
    stream("tool", { tool_started: "computer" });
    stream("tool", { tool_error: "Page unavailable", state: "error" });
    // Step metadata on tools is supplied in the persisted timeline projection.
    message.parts.find((entry) => entry.id === "tool")!.meta = { step: 1 };
    expect(buildAgentStepViews(message.parts)[0]!.status).toBe("error");
    stream("next-plan", { agent: "Retry" }, 2);
    expect(
      buildAgentStepViews(message.parts, { finish: "aborted" }).map(
        (view) => view.status,
      ),
    ).toEqual(["error", "error"]);
  });

  it("accepts frozen snapshots without mutating or replacing parts", () => {
    const plan = part("agent", 1);
    Object.freeze(plan.meta);
    Object.freeze(plan);
    const parts = [plan, part("step-start", 1)];
    Object.freeze(parts);
    const before = JSON.stringify(parts);
    expect(buildAgentStepViews(parts)[0]!.part).toBe(plan);
    expect(hasRunningAgentSequence(parts)).toBe(true);
    expect(JSON.stringify(parts)).toBe(before);
  });
});

describe("Agent-S projection work bound", () => {
  it.each([50, 200])(
    "visits parts linearly for %i completed steps",
    (count) => {
      let reads = 0;
      const parts = Array.from({ length: count }, (_, step) =>
        ["step-start", "reasoning", "agent", "tool", "step-finish"].map(
          (type) => {
            const entry = part(type as HarnessPart["type"], step);
            return new Proxy(entry, {
              get(target, key, receiver) {
                if (key === "type") reads += 1;
                return Reflect.get(target, key, receiver);
              },
            });
          },
        ),
      ).flat();
      expect(buildAgentStepViews(parts)).toHaveLength(count);
      expect(reads).toBeLessThan(parts.length * 12);
      reads = 0;
      expect(hasRunningAgentSequence(parts)).toBe(false);
      expect(reads).toBeLessThan(parts.length * 12);
    },
  );
});
