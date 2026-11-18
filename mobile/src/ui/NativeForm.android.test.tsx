import React from "react";
import { act, render, screen } from "@testing-library/react-native";
import type { ReactTestRendererJSON } from "react-test-renderer";
import { NativeForm } from "./NativeForm.android";
import type { NativeFormProps } from "./NativeForm.types";
import { ThemeProvider } from "./theme";

// Every modifier records its name and arguments; `Shapes.*` nest.
jest.mock("@expo/ui/jetpack-compose/modifiers", () => {
  const modifier = (name: string): unknown =>
    new Proxy((...args: unknown[]) => ({ name, args }), {
      get: (target, prop, receiver) =>
        typeof prop === "string" && /^[A-Z]/.test(prop)
          ? modifier(`${name}.${prop}`)
          : Reflect.get(target, prop, receiver),
    });
  return new Proxy(
    {},
    {
      get: (_target, name) =>
        name === "__esModule" ? true : modifier(String(name)),
    },
  );
});

// Compose views become host elements named after the view (`ListItem`,
// `ListItem.TrailingContent`, ...), keeping their props.
jest.mock("@expo/ui/jetpack-compose", () => {
  const { createElement } = require("react");
  const view = (name: string): unknown =>
    new Proxy(
      ({ children, ...props }: React.PropsWithChildren) =>
        createElement(name, props, children),
      {
        get: (target, prop, receiver) =>
          typeof prop === "string" && /^[A-Z]/.test(prop)
            ? view(`${name}.${prop}`)
            : Reflect.get(target, prop, receiver),
      },
    );
  return new Proxy(
    {},
    {
      get: (_target, name) =>
        name === "__esModule"
          ? true
          : name === "useNativeState"
            ? (value: unknown) => ({ value, set: () => undefined })
            : view(String(name)),
    },
  );
});

// Metro bundles the vector XMLs; Jest only needs a source per symbol.
jest.mock("./androidIcons", () => ({
  WORKSPACE_ICON: 0,
  androidIcon: (symbol?: string) => (symbol ? 1 : undefined),
}));

type Node = ReactTestRendererJSON;
type Modifier = { name: string; args: unknown[] };

function hostChildren(node: Node): Node[] {
  return (node.children ?? []).filter(
    (child): child is Node => typeof child !== "string",
  );
}

/** Every host element below `root`, optionally of one type. */
function nodes(root: Node | Node[] | null, type?: string): Node[] {
  if (!root) return [];
  if (Array.isArray(root)) return root.flatMap((node) => nodes(node, type));
  return [
    ...(!type || root.type === type ? [root] : []),
    ...hostChildren(root).flatMap((child) => nodes(child, type)),
  ];
}

/** The control a test ID modifier marks, with its spoken name. */
function control(testID: string) {
  const match = nodes(screen.toJSON()).find((node) =>
    (node.props.modifiers as Modifier[] | undefined)?.some(
      (modifier) => modifier.name === "testID" && modifier.args[0] === testID,
    ),
  );
  if (!match) throw new Error(`No control with test ID ${testID}`);
  const semantics = (match.props.modifiers as Modifier[]).find(
    (modifier) => modifier.name === "semantics",
  );
  return {
    type: match.type,
    label: (semantics?.args[0] as { contentDescription?: string })
      ?.contentDescription,
  };
}

function renderForm(props: NativeFormProps) {
  return render(
    <ThemeProvider initialMode="light">
      <NativeForm {...props} />
    </ThemeProvider>,
  );
}

describe("NativeForm (Android)", () => {
  it("renders each section as one lazy item", () => {
    const sections = (repositories: string[]): NativeFormProps["sections"] => [
      {
        id: "repositories",
        title: "Repositories",
        footer: "Cloned into the workspace.",
        rows: repositories.map((url, index) => ({
          kind: "text" as const,
          id: `repository-${index}`,
          label: "Repository",
          value: url,
          onChange: () => undefined,
        })),
      },
      {
        id: "name",
        rows: [
          {
            kind: "text",
            id: "workspace-name",
            label: "Name",
            value: "",
            onChange: () => undefined,
          },
        ],
      },
    ];
    const view = renderForm({ sections: sections(["git@a"]) });
    const items = () => hostChildren(nodes(screen.toJSON(), "LazyColumn")[0]!);
    expect(items().map((item) => item.type)).toEqual(["Column", "Column"]);

    // An added row stays inside its section's item, so later items keep
    // their position (and a focused field its focus).
    view.rerender(
      <ThemeProvider initialMode="light">
        <NativeForm sections={sections(["git@a", "git@b"])} />
      </ThemeProvider>,
    );
    expect(items()).toHaveLength(2);
    expect(
      items().map((item) => nodes(item, "OutlinedTextField").length),
    ).toEqual([2, 1]);
  });

  it("keeps the name and test ID of a busy accessory", () => {
    renderForm({
      sections: [
        {
          id: "apps",
          rows: [
            {
              kind: "item",
              id: "app-1",
              title: "Notes",
              accessory: {
                title: "Install",
                accessibilityLabel: "Install Notes",
                testID: "app-action-1",
                busy: true,
                onPress: () => undefined,
              },
            },
          ],
        },
      ],
    });
    expect(control("app-action-1")).toEqual({
      type: "Box",
      label: "Install Notes",
    });
  });

  it("keeps the name and test ID of a busy empty-state action", () => {
    renderForm({
      sections: [],
      empty: {
        title: "Couldn’t load apps",
        symbol: "exclamationmark.triangle",
        action: {
          title: "Retry",
          testID: "apps-retry",
          busy: true,
          onPress: () => undefined,
        },
      },
    });
    expect(control("apps-retry")).toEqual({ type: "Box", label: "Retry" });
  });

  it("opens the date dialog without the seeded text-input mode", () => {
    renderForm({
      sections: [
        {
          id: "schedule",
          rows: [
            {
              kind: "date",
              id: "run-date",
              label: "Date",
              mode: "date",
              value: "2026-11-17",
              onChange: () => undefined,
            },
          ],
        },
      ],
    });
    const row = nodes(screen.toJSON(), "ListItem")[0]!;
    const open = (row.props.modifiers as Modifier[]).find(
      (modifier) => modifier.name === "clickable",
    )!.args[0] as () => void;
    act(open);
    expect(
      nodes(screen.toJSON(), "DatePickerDialog")[0]?.props.showVariantToggle,
    ).toBe(false);
  });
});
