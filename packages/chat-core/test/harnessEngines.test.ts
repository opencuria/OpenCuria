import { describe, expect, it } from "vitest";
import {
  harnessEngineDefaults,
  harnessEngineLabel,
  harnessSessionModels,
  formatEffort,
  snapEffort,
  type HarnessEngine,
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
  it("uses only backend engine defaults and native agent configuration", () => {
    const engines: HarnessEngine[] = [
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
    expect(harnessEngineDefaults("claude", engines)).toEqual({ model: "", effort: "" });
    expect(harnessEngineDefaults("codex", [])).toEqual({ model: "", effort: "" });
    expect(harnessEngineLabel("codex", [])).toBe("codex");
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
