import React from "react";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { ProviderModel } from "@opencuria/chat-core/harnessModels";
import { ModelSheet } from "./ModelSheet";
import { ThemeProvider } from "../ui/theme";

const model = (id: string, extra: Partial<ProviderModel> = {}) => ({
  id,
  name: `Model ${id}`,
  provider: "openrouter",
  reasoning_efforts: [],
  default_effort: "",
  supports_tools: true,
  context_length: 200_000,
  max_output_tokens: 8_000,
  ...extra,
});
const catalog: ProviderModel[] = [
  model("a", { reasoning_efforts: ["low", "high"], default_effort: "low" }),
  ...["b", "c", "d", "e", "f", "g", "h"].map((id) => model(id)),
];

function renderSheet(
  props: Partial<React.ComponentProps<typeof ModelSheet>> = {},
) {
  const handlers = {
    onClose: jest.fn(),
    onModelChange: jest.fn(),
    onEffortChange: jest.fn(),
    onAgentChange: jest.fn(),
  };
  render(
    <ThemeProvider>
      <ModelSheet
        visible
        models={catalog}
        recentModelIds={["h", "missing"]}
        model="a"
        effort="high"
        agents={[
          { engine: "native", mode: "build", label: "OpenCuria Build" },
          { engine: "native", mode: "plan", label: "OpenCuria Plan" },
        ]}
        engine="native"
        mode="build"
        {...handlers}
        {...props}
      />
    </ThemeProvider>,
  );
  return handlers;
}

describe("ModelSheet", () => {
  it("lists recent models first and the selected model's efforts", () => {
    const { onEffortChange, onAgentChange } = renderSheet();
    const rows = screen
      .getAllByRole("button")
      .map((row) => row.props.accessibilityLabel as string);
    expect(rows.slice(0, 7)).toEqual([
      "Model h, OpenRouter · 200k context",
      "Model a, OpenRouter · 200k context",
      "Model b, OpenRouter · 200k context",
      "Model c, OpenRouter · 200k context",
      "Model d, OpenRouter · 200k context",
      "Model e, OpenRouter · 200k context",
      "All models, 8 available",
    ]);
    expect(
      screen.getByRole("button", { name: /^Model a/ }).props.accessibilityState
        .selected,
    ).toBe(true);
    fireEvent.press(screen.getByText("Low"));
    expect(onEffortChange).toHaveBeenCalledWith("low");
    fireEvent.press(screen.getByText("OpenCuria Plan"));
    expect(onAgentChange).toHaveBeenCalledWith("native", "plan");
  });

  it("searches the full catalog and returns to the overview after a choice", () => {
    const { onModelChange } = renderSheet();
    fireEvent.press(screen.getByText("All models"));
    fireEvent.changeText(screen.getByLabelText("Search models"), "model g");
    expect(screen.queryByText("Model b")).toBeNull();
    fireEvent.press(screen.getByText("Model g"));
    expect(onModelChange).toHaveBeenCalledWith("g");
    expect(screen.queryByLabelText("Search models")).toBeNull();
    expect(screen.getByText("Effort")).toBeTruthy();
  });

  it("explains an empty engine catalog instead of listing models", () => {
    renderSheet({
      models: [],
      unavailableMessage: "Codex models unavailable.",
    });
    expect(screen.getByText("Codex models unavailable.")).toBeTruthy();
    expect(screen.queryByText("Effort")).toBeNull();
  });
});
