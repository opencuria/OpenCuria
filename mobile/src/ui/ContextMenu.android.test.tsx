import React from "react";
import { Animated, Pressable, Text } from "react-native";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { NativeContextMenu } from "./ContextMenu.android";
import { gestureThresholdHaptic } from "./haptics";
import { ThemeProvider } from "./theme";

jest.mock("./haptics", () => ({ gestureThresholdHaptic: jest.fn() }));
jest.mock("@expo/ui/jetpack-compose/modifiers", () => ({
  fillMaxSize: () => ({ name: "fillMaxSize" }),
}));
// Compose views become host elements named after the view, keeping props.
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
        name === "__esModule" ? true : view(String(name)),
    },
  );
});

it("ticks and lifts the content while its menu is open", () => {
  const spring = jest.spyOn(Animated, "spring");
  const onSelect = jest.fn();
  render(
    <ThemeProvider>
      <NativeContextMenu
        label="Prompt"
        actions={[{ id: "copy", title: "Copy" }]}
        onSelect={onSelect}
      >
        {(showMenu) => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Prompt"
            onLongPress={showMenu}
          >
            <Text>Prompt</Text>
          </Pressable>
        )}
      </NativeContextMenu>
    </ThemeProvider>,
  );
  fireEvent(screen.root, "layout", {
    nativeEvent: { layout: { x: 0, y: 0, width: 400, height: 48 } },
  });
  fireEvent(screen.getByRole("button", { name: "Prompt" }), "longPress");

  expect(gestureThresholdHaptic).toHaveBeenCalledTimes(1);
  // 6 pt on each side of 400 pt.
  expect(spring).toHaveBeenLastCalledWith(
    expect.anything(),
    expect.objectContaining({ toValue: 1.03 }),
  );
  const [item] = screen.UNSAFE_root.findAll(
    (node) => node.type === ("DropdownMenuItem" as never),
  );
  act(() => item?.props.onClick());
  expect(onSelect).toHaveBeenCalledWith("copy");
  expect(spring).toHaveBeenLastCalledWith(
    expect.anything(),
    expect.objectContaining({ toValue: 1 }),
  );
  spring.mockRestore();
});
