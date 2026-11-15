import { describe, expect, it } from "vitest";
import {
  windowsActivationLabel,
  workspaceWindowsView,
} from "../src/windowsLicense";

const license = (activation_status: string, activation_code = "") => ({
  kind: "device_key",
  activation_status,
  activation_code,
});

describe("Windows workspace license view", () => {
  it("has no label for Linux and legacy workspaces", () => {
    expect(workspaceWindowsView({ status: "running" })).toBeNull();
    expect(
      workspaceWindowsView({ status: "running", guest_os: "linux" }),
    ).toBeNull();
  });

  it("labels each activation state", () => {
    const view = (status: string, code = "", workspaceStatus = "running") =>
      workspaceWindowsView({
        status: workspaceStatus,
        guest_os: "windows",
        windows_license: license(status, code),
      });
    expect(view("activated")).toEqual({
      os: "Windows",
      license: { label: "Activated", tone: "ok" },
      text: "Windows · Activated",
    });
    expect(view("failed", "0xC004F074")?.text).toBe(
      "Windows · Activation failed (0xC004F074)",
    );
    expect(view("failed")?.license).toEqual({
      label: "Activation failed",
      tone: "attention",
    });
    expect(view("not_activated")?.license).toEqual({
      label: "Not activated",
      tone: "muted",
    });
    expect(view("pending")?.license).toEqual({
      label: "Activating…",
      tone: "busy",
    });
  });

  it("does not claim activity for a pending workspace that is not running", () => {
    expect(
      workspaceWindowsView({
        status: "stopped",
        guest_os: "windows",
        windows_license: license("pending"),
      })?.text,
    ).toBe("Windows · Activation pending");
    expect(windowsActivationLabel("pending").label).toBe("Activation pending");
  });

  it("shows only the OS while the server reports no license state", () => {
    expect(
      workspaceWindowsView({
        status: "running",
        guest_os: "windows",
        windows_license: null,
      }),
    ).toEqual({ os: "Windows", license: null, text: "Windows" });
  });

  it("reads unknown states as not activated", () => {
    expect(windowsActivationLabel("something_new", "", true)).toEqual({
      label: "Not activated",
      tone: "muted",
    });
  });
});
