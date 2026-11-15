import React from "react";
import { Text } from "react-native";
import { fireEvent, render, screen } from "@testing-library/react-native";
import { KeyboardController } from "react-native-keyboard-controller";
import { SlideDrawer } from "./SlideDrawer";
import { ThemeProvider } from "./theme";

function setup(open: boolean) {
  const props = { onOpen: jest.fn(), onClose: jest.fn() };
  const tree = (value: boolean) => (
    <ThemeProvider>
      <SlideDrawer open={value} {...props} sidebar={<Text>Sidebar</Text>}>
        <Text>Chat</Text>
      </SlideDrawer>
    </ThemeProvider>
  );
  const result = render(tree(open));
  return {
    ...props,
    rerender: (value: boolean) => result.rerender(tree(value)),
  };
}

it("hides the closed sidebar from assistive technology and touches", () => {
  setup(false);
  const sidebar = screen.getByTestId("conversation-drawer", {
    includeHiddenElements: true,
  });
  expect(screen.queryByText("Sidebar")).toBeNull();
  expect(sidebar.props.importantForAccessibility).toBe("no-hide-descendants");
  expect(sidebar.props.pointerEvents).toBe("none");
  expect(screen.queryByRole("button", { name: "Close sidebar" })).toBeNull();
});

it("closes the keyboard on open and closes from the visible chat edge", () => {
  const { onClose, rerender } = setup(false);
  rerender(true);
  expect(KeyboardController.dismiss).toHaveBeenCalled();
  expect(
    screen.getByTestId("conversation-drawer").props.importantForAccessibility,
  ).toBe("auto");
  fireEvent.press(screen.getByRole("button", { name: "Close sidebar" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});
