import { Modal, Text } from "react-native";
import { render } from "@testing-library/react-native";
import { NativeModal } from "./NativeModal";
import { ThemeProvider } from "./theme";

jest.mock("lucide-react-native", () => ({ X: () => null }));

it("uses native page presentation and lets the system close a dismissible screen", () => {
  const onClose = jest.fn();
  const result = render(
    <ThemeProvider>
      <NativeModal visible onClose={onClose}>
        <Text>Settings</Text>
      </NativeModal>
    </ThemeProvider>,
  );
  const modal = result.UNSAFE_getByType(Modal);
  expect(modal.props.presentationStyle).toBe("pageSheet");
  expect(modal.props.transparent).not.toBe(true);
  modal.props.onRequestClose();
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("prevents native swipe/back dismissal of unresolved gates", () => {
  const onClose = jest.fn();
  const result = render(
    <ThemeProvider>
      <NativeModal visible dismissible={false} onClose={onClose}>
        <Text>Permission required</Text>
      </NativeModal>
    </ThemeProvider>,
  );
  const modal = result.UNSAFE_getByType(Modal);
  expect(modal.props.allowSwipeDismissal).toBe(false);
  modal.props.onRequestClose();
  expect(onClose).not.toHaveBeenCalled();
  expect(result.queryByRole("button", { name: "Close" })).toBeNull();
});
