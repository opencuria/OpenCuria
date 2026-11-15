import React from "react";
import {
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react-native";
import type { ProviderModel } from "@opencuria/chat-core/harnessModels";
import { ModelSheet, type ModelSheetEngine } from "./ModelSheet";
import { ThemeProvider } from "../ui/theme";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);

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
  model("a", {
    reasoning_efforts: ["low", "medium", "high", "xhigh"],
    default_effort: "medium",
  }),
  ...["b", "c", "d", "e", "f", "g", "h"].map((id) => model(id)),
];
const native: ModelSheetEngine = {
  id: "native",
  name: "OpenCuria",
  modes: ["build", "plan"],
};

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
        engines={[native]}
        engine="native"
        mode="build"
        {...handlers}
        {...props}
      />
    </ThemeProvider>,
  );
  return handlers;
}

const radios = (testID: string) =>
  within(screen.getByTestId(testID))
    .getAllByRole("radio")
    .map((item) => ({
      name: item.props.accessibilityLabel as string,
      checked: Boolean(item.props.accessibilityState?.checked),
    }));

describe("ModelSheet", () => {
  it("lists recent models first with the full catalog on demand", () => {
    renderSheet();
    expect(screen.getByText("Recent")).toBeTruthy();
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
  });

  it("pins the selected model's efforts as compact segments with spoken names", () => {
    const { onEffortChange } = renderSheet();
    expect(radios("model-sheet-effort")).toEqual([
      { name: "Low", checked: false },
      { name: "Medium", checked: false },
      { name: "High", checked: true },
      { name: "Extra High", checked: false },
    ]);
    const effort = within(screen.getByTestId("model-sheet-effort"));
    expect(effort.getByText("Med")).toBeTruthy();
    expect(effort.getByText("X-High")).toBeTruthy();
    fireEvent.press(effort.getByRole("radio", { name: "Extra High" }));
    expect(onEffortChange).toHaveBeenCalledWith("xhigh");
  });

  it("switches the mode of the current engine and hides a single engine", () => {
    const { onAgentChange } = renderSheet();
    expect(radios("model-sheet-mode")).toEqual([
      { name: "Build", checked: true },
      { name: "Plan", checked: false },
    ]);
    expect(screen.queryByTestId("model-sheet-engine")).toBeNull();
    fireEvent.press(screen.getByRole("radio", { name: "Plan" }));
    expect(onAgentChange).toHaveBeenCalledWith("native", "plan");
  });

  it("offers other engines in a menu, keeping a supported mode", () => {
    const { onAgentChange } = renderSheet({
      mode: "plan",
      engines: [
        native,
        {
          id: "claude",
          name: "Claude Agent",
          modes: ["build", "plan"],
          disabled: true,
          note: "Not connected",
        },
        { id: "codex", name: "Codex", modes: ["build"] },
      ],
    });
    fireEvent.press(screen.getByLabelText("Engine: OpenCuria"));
    expect(
      screen.getByRole("button", { name: "Claude Agent" }).props
        .accessibilityState.disabled,
    ).toBe(true);
    fireEvent.press(screen.getByRole("button", { name: "Codex" }));
    expect(onAgentChange).toHaveBeenCalledWith("codex", "build");
  });

  it("searches the full catalog and returns to the overview after a choice", () => {
    const { onModelChange } = renderSheet();
    fireEvent.press(screen.getByText("All models"));
    expect(screen.queryByTestId("model-sheet-effort")).toBeNull();
    fireEvent.changeText(screen.getByLabelText("Search models"), "model g");
    expect(screen.queryByText("Model b")).toBeNull();
    fireEvent.press(screen.getByText("Model g"));
    expect(onModelChange).toHaveBeenCalledWith("g");
    expect(screen.queryByLabelText("Search models")).toBeNull();
    expect(screen.getByTestId("model-sheet-effort")).toBeTruthy();
  });

  it("explains an empty engine catalog instead of listing models", () => {
    renderSheet({
      models: [],
      unavailableMessage: "Codex models unavailable.",
    });
    expect(screen.getByText("Codex models unavailable.")).toBeTruthy();
    expect(screen.queryByTestId("model-sheet-effort")).toBeNull();
  });
});
