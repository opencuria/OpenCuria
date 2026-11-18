import { Modal, Text } from "react-native";
import { render } from "@testing-library/react-native";
import { NativeModal } from "./NativeModal";
import type { NativeFormProps } from "./NativeForm.types";
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

const emptyForm: NativeFormProps = {
  sections: [],
  empty: {
    title: "No workspaces yet",
    symbol: "square.stack.3d.up",
    action: { title: "New workspace", onPress: jest.fn() },
  },
};

function renderWithForm(form: NativeFormProps) {
  return render(
    <ThemeProvider>
      <NativeModal
        visible
        onClose={jest.fn()}
        title="Workspaces"
        primaryAction={{
          title: "New workspace",
          symbol: "plus",
          testID: "toolbar-new-workspace",
          onPress: jest.fn(),
        }}
        form={form}
      >
        <Text>Nested sheet</Text>
      </NativeModal>
    </ThemeProvider>,
  );
}

it("renders the form in the sheet and keeps children as overlays", () => {
  const result = renderWithForm({
    sections: [
      { id: "all", rows: [{ kind: "item", id: "alpha", title: "Alpha" }] },
    ],
  });
  expect(result.getByText("Alpha")).toBeTruthy();
  expect(result.getByText("Nested sheet")).toBeTruthy();
  expect(result.getByTestId("toolbar-new-workspace")).toBeTruthy();
});

it("hides the main action while the empty state offers it", () => {
  const result = renderWithForm(emptyForm);
  expect(result.getByText("No workspaces yet")).toBeTruthy();
  expect(result.getAllByRole("button", { name: "New workspace" })).toHaveLength(
    1,
  );
  expect(result.queryByTestId("toolbar-new-workspace")).toBeNull();
});

it("keeps the main action while the form is still loading", () => {
  const result = renderWithForm({ ...emptyForm, loading: true });
  expect(result.getByTestId("toolbar-new-workspace")).toBeTruthy();
});
