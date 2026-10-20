import { describe, expect, it } from "vitest";
import {
  harnessEngineChoices,
  harnessEngineDefaults,
  harnessEngineLabel,
  harnessSessionModels,
  formatEffort,
  snapEffort,
  type ProviderModel,
} from "../src/harnessModels";

const models: ProviderModel[] = [
  {
    id: "chatgpt/gpt-6.1-sol",
    name: "Sol",
    provider: "chatgpt",
    reasoning_efforts: ["low", "ultra"],
    default_effort: "low",
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
  {
    id: "openrouter/vendor/model",
    name: "Router",
    provider: "openrouter",
    reasoning_efforts: ["high"],
    default_effort: "high",
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
];

describe("shared engine policy", () => {
  it("uses backend labels/defaults and native agent defaults, retaining legacy Claude fixtures", () => {
    const engines = [
      {
        id: "codex",
        name: "Codex Runtime",
        connected: true,
        modes: ["plan"],
        default_model: "openrouter/vendor/model",
        default_effort: "high",
      },
    ];
    expect(harnessEngineLabel("codex", engines)).toBe("Codex Runtime");
    expect(
      harnessEngineChoices(engines).find((e) => e.id === "codex")?.modes,
    ).toEqual(["plan"]);
    expect(harnessEngineDefaults("codex", engines)).toEqual({
      model: "openrouter/vendor/model",
      effort: "high",
    });
    expect(
      harnessEngineDefaults("native", engines, {
        model: "agent/model",
        effort: "medium",
      }),
    ).toEqual({ model: "agent/model", effort: "medium" });
    expect(harnessEngineDefaults("claude")).toEqual({
      model: "sonnet",
      effort: "high",
    });
    expect(harnessEngineDefaults("codex")).toEqual({
      model: "chatgpt/gpt-6.1-sol",
      effort: "low",
    });
    expect(
      harnessEngineChoices([]).find((e) => e.id === "codex")?.connected,
    ).toBe(false);
  });

  it("allows both backend-supplied providers in new chats but locks an existing Codex provider", () => {
    expect(harnessSessionModels(models, "codex")).toEqual(models);
    expect(harnessSessionModels(models, "codex", "chatgpt/old")).toEqual([
      models[0],
    ]);
    expect(
      harnessSessionModels(models, "codex", "openrouter/vendor/old"),
    ).toEqual([models[1]]);
    expect(harnessSessionModels(models, "codex", "")).toEqual([]);
    expect(harnessSessionModels(models, "native", models[0]!.id)).toEqual(
      models,
    );
  });

  it("uses runtime effort metadata, including ultra only on a supporting model", () => {
    expect(formatEffort("ultra")).toBe("Ultra");
    expect(formatEffort("future-token")).toBe("future-token");
    expect(snapEffort(models[0], "ultra")).toBe("ultra");
    expect(snapEffort(models[1], "ultra")).toBe("high");
  });
});
