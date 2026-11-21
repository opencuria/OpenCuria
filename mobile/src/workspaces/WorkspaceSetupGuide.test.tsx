import React from "react";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { OpenCuriaClient } from "@opencuria/client";
import { ThemeProvider } from "../ui/theme";
import { WorkspaceSetupGuide } from "./WorkspaceSetupGuide";

jest.mock(
  "lucide-react-native",
  () => new Proxy({}, { get: () => () => null }),
);
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, right: 0, bottom: 0, left: 0 }),
}));

const ubuntu = {
  id: "def-1",
  name: "Ubuntu 24.04",
  is_active: true,
  status: "active",
};

function mockClient({
  runnerStatus = "online",
  built = false,
}: { runnerStatus?: string; built?: boolean } = {}) {
  return {
    runners: {
      list: jest
        .fn()
        .mockResolvedValue([
          { id: "runner-1", name: "host-1", status: runnerStatus },
        ]),
    },
    plugins: { list: jest.fn().mockResolvedValue([]) },
    credentials: { list: jest.fn().mockResolvedValue([]) },
    images: {
      listCaptured: jest.fn().mockResolvedValue([]),
      listDefinitions: jest.fn().mockResolvedValue([ubuntu]),
      listRunnerBuilds: jest
        .fn()
        .mockResolvedValue(
          built
            ? [
                {
                  runner_id: "runner-1",
                  status: "active",
                  image_artifact_id: "artifact-1",
                },
              ]
            : [],
        ),
    },
  } as unknown as OpenCuriaClient;
}

function renderGuide(
  props: Partial<React.ComponentProps<typeof WorkspaceSetupGuide>> = {},
) {
  const onOpenSettings = jest.fn();
  const view = render(
    <ThemeProvider>
      <WorkspaceSetupGuide
        client={mockClient()}
        organizationId="org-1"
        isAdmin
        providerConnected={false}
        refreshKey={0}
        onOpenSettings={onOpenSettings}
        onCreated={jest.fn().mockResolvedValue(undefined)}
        {...props}
      />
    </ThemeProvider>,
  );
  return { ...view, onOpenSettings };
}

describe("WorkspaceSetupGuide", () => {
  it("offers the provider step while the image still has to be built", async () => {
    const { onOpenSettings } = renderGuide();
    expect(await screen.findByText("Build Ubuntu 24.04 on your runner.")).toBeTruthy();
    expect(screen.getByTestId("workspace-setup-progress")).toHaveTextContent(
      "1 of 4",
    );
    expect(screen.queryByTestId("workspace-setup-action-workspace")).toBeNull();

    fireEvent.press(screen.getByTestId("workspace-setup-action-provider"));
    expect(onOpenSettings).toHaveBeenCalledWith(["providers"]);
    fireEvent.press(screen.getByTestId("workspace-setup-action-image"));
    expect(onOpenSettings).toHaveBeenCalledWith(["image-definitions"]);
  });

  it("lets the workspace be created once an image is ready", async () => {
    renderGuide({ client: mockClient({ built: true }), providerConnected: true });
    expect(await screen.findByText("Ubuntu 24.04 is ready.")).toBeTruthy();
    expect(screen.getByText("Models are ready for your agents.")).toBeTruthy();
    expect(screen.getByTestId("workspace-setup-progress")).toHaveTextContent(
      "3 of 4",
    );
    expect(screen.getByTestId("workspace-setup-action-workspace")).toBeTruthy();
  });

  it("shows members only the steps they can take themselves", async () => {
    renderGuide({ isAdmin: false });
    expect(
      await screen.findByText("Available as soon as a workspace image is ready."),
    ).toBeTruthy();
    expect(screen.queryByTestId("workspace-setup-step-runner")).toBeNull();
    expect(screen.queryByTestId("workspace-setup-step-image")).toBeNull();
    expect(screen.queryByTestId("workspace-setup-action-workspace")).toBeNull();
    expect(screen.getByTestId("workspace-setup-action-provider")).toBeTruthy();
  });
});
