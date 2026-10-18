import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { ThemeProvider } from "../ui/theme";
import { LoginScreen } from "./LoginScreen";
import { useSession } from "./SessionContext";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  ...jest.requireActual("react-native-safe-area-context"),
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));
jest.mock("./SessionContext", () => ({
  ...jest.requireActual("./SessionContext"),
  useSession: jest.fn(),
}));

const mockUseSession = useSession as jest.MockedFunction<typeof useSession>;
const login = jest.fn().mockResolvedValue(undefined);

function mockSession(error: string | null = null) {
  mockUseSession.mockReturnValue({
    login,
    error,
    clearError: jest.fn(),
    booting: false,
  } as never);
}

function renderLogin() {
  mockSession();
  return render(
    <ThemeProvider initialMode="light">
      <LoginScreen />
    </ThemeProvider>,
  );
}

beforeEach(() => login.mockClear());

describe("LoginScreen", () => {
  it("asks for the server first when none is configured", () => {
    renderLogin();
    expect(screen.getByTestId("login-server")).toBeTruthy();
    expect(screen.getByText("Set server")).toBeTruthy();
    expect(screen.getByTestId("login-submit")).toBeDisabled();
  });

  it("collapses a known server into a quiet label", () => {
    renderLogin();
    fireEvent.changeText(screen.getByTestId("login-server"), "curia.example");
    expect(screen.getByText("curia.example")).toBeTruthy();
    fireEvent.press(screen.getByTestId("login-server-toggle"));
    expect(screen.queryByTestId("login-server")).toBeNull();
    fireEvent.press(screen.getByTestId("login-server-toggle"));
    expect(screen.getByTestId("login-server")).toBeTruthy();
  });

  it("marks a local plain-HTTP server and signs in with it", async () => {
    renderLogin();
    fireEvent.changeText(
      screen.getByTestId("login-server"),
      "192.168.1.20:8000",
    );
    expect(screen.getByText("http://192.168.1.20:8000")).toBeTruthy();
    fireEvent.changeText(screen.getByTestId("login-email"), " me@example.com ");
    fireEvent.changeText(screen.getByTestId("login-password"), "secret");
    fireEvent.press(screen.getByTestId("login-submit"));
    await waitFor(() =>
      expect(login).toHaveBeenCalledWith(
        "192.168.1.20:8000",
        " me@example.com ",
        "secret",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("login-submit")).toBeEnabled(),
    );
  });

  it("reopens a hidden server field when its URL is rejected", () => {
    const view = renderLogin();
    fireEvent.changeText(
      screen.getByTestId("login-server"),
      "http://example.com",
    );
    fireEvent.press(screen.getByTestId("login-server-toggle"));
    expect(screen.queryByTestId("login-server")).toBeNull();

    mockSession("Use https://");
    view.rerender(
      <ThemeProvider initialMode="light">
        <LoginScreen />
      </ThemeProvider>,
    );
    expect(screen.getByTestId("login-server")).toBeTruthy();
    expect(screen.getByTestId("login-error")).toBeTruthy();
  });

  it("toggles password visibility", () => {
    renderLogin();
    const password = screen.getByTestId("login-password");
    expect(password.props.secureTextEntry).toBe(true);
    fireEvent.press(screen.getByLabelText("Show password"));
    expect(screen.getByTestId("login-password").props.secureTextEntry).toBe(
      false,
    );
  });
});
