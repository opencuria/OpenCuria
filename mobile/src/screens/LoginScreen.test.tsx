import React from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import { rejectHaptic } from "../ui/haptics";
import { ThemeProvider } from "../ui/theme";
import { LoginScreen } from "./LoginScreen";
import { LoginStage } from "./LoginStage";
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
jest.mock("../ui/haptics", () => ({ rejectHaptic: jest.fn() }));
jest.mock("./LoginStage", () => ({
  STAGE_GROUND: "#0B0A14",
  LoginStage: jest.fn(() => null),
}));

const mockUseSession = useSession as jest.MockedFunction<typeof useSession>;
const mockStage = LoginStage as jest.MockedFunction<typeof LoginStage>;
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

function stage() {
  return mockStage.mock.calls[mockStage.mock.calls.length - 1][0];
}

beforeEach(() => {
  login.mockClear();
  mockStage.mockClear();
  (rejectHaptic as jest.Mock).mockClear();
});

describe("LoginScreen", () => {
  it("asks for the server first when none is configured", () => {
    renderLogin();
    expect(screen.getByTestId("login-server")).toBeTruthy();
    expect(screen.getByText("your server")).toBeTruthy();
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

  it("signals a rejected sign-in, but not an error it opened with", async () => {
    mockSession("Your session expired.");
    const view = render(
      <ThemeProvider initialMode="light">
        <LoginScreen />
      </ThemeProvider>,
    );
    expect(rejectHaptic).not.toHaveBeenCalled();

    fireEvent.changeText(screen.getByTestId("login-server"), "curia.example");
    fireEvent.changeText(screen.getByTestId("login-email"), "me@example.com");
    fireEvent.changeText(screen.getByTestId("login-password"), "wrong");
    fireEvent.press(screen.getByTestId("login-submit"));
    await waitFor(() => expect(login).toHaveBeenCalled());
    mockSession("Invalid email or password.");
    view.rerender(
      <ThemeProvider initialMode="light">
        <LoginScreen />
      </ThemeProvider>,
    );
    expect(rejectHaptic).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Invalid email or password.")).toBeTruthy();
  });

  it("lights a side of the mark for each completed part", () => {
    renderLogin();
    expect(stage()).toMatchObject({
      server: false,
      email: false,
      password: false,
    });
    fireEvent.changeText(screen.getByTestId("login-server"), "curia.example");
    expect(stage()).toMatchObject({ server: true, email: false });
    fireEvent.changeText(screen.getByTestId("login-email"), "   ");
    expect(stage().email).toBe(false);
    fireEvent.changeText(screen.getByTestId("login-email"), "me@example.com");
    fireEvent.changeText(screen.getByTestId("login-password"), "secret");
    expect(stage()).toMatchObject({
      server: true,
      email: true,
      password: true,
      working: false,
    });
  });

  it("sets the mark to work while signing in", async () => {
    let finish = () => undefined as void;
    login.mockImplementationOnce(
      () => new Promise<void>((resolve) => (finish = resolve)),
    );
    renderLogin();
    fireEvent.changeText(screen.getByTestId("login-server"), "curia.example");
    fireEvent.changeText(screen.getByTestId("login-email"), "me@example.com");
    fireEvent.changeText(screen.getByTestId("login-password"), "secret");
    fireEvent.press(screen.getByTestId("login-submit"));
    await waitFor(() => expect(stage().working).toBe(true));
    finish();
    await waitFor(() => expect(stage().working).toBe(false));
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
