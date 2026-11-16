/**
 * How a workspace's guest OS and Windows license state read in the web and
 * native clients. Linux workspaces have no label. The server owns licensing;
 * these rules only decide the wording.
 */

/** Minimal workspace fields the license label reads. */
export interface WorkspaceGuestInput {
  status?: string;
  guest_os?: string | null;
  windows_license?: {
    activation_status: string;
    activation_code?: string | null;
  } | null;
}

/** Visual weight of a license state, independent of any color system. */
export type WindowsLicenseTone = "ok" | "busy" | "attention" | "muted";

export interface WindowsActivationView {
  label: string;
  tone: WindowsLicenseTone;
}

export interface WorkspaceWindowsView {
  os: "Windows";
  /** Activation state, or null while the server reports none. */
  license: WindowsActivationView | null;
  /** "Windows" or "Windows · Activated". */
  text: string;
}

/**
 * "Activated", "Activation failed (0xC004F074)", "Not activated", or
 * "Activating…" while a running workspace waits for its activation result
 * ("Activation pending" otherwise).
 */
export function windowsActivationLabel(
  status: string,
  code = "",
  running = false,
): WindowsActivationView {
  switch (status) {
    case "activated":
      return { label: "Activated", tone: "ok" };
    case "failed":
      return {
        label: code ? `Activation failed (${code})` : "Activation failed",
        tone: "attention",
      };
    case "pending":
      return running
        ? { label: "Activating…", tone: "busy" }
        : { label: "Activation pending", tone: "muted" };
    default:
      return { label: "Not activated", tone: "muted" };
  }
}

/** OS and license state of a Windows workspace; null for Linux workspaces. */
export function workspaceWindowsView(
  workspace: WorkspaceGuestInput,
): WorkspaceWindowsView | null {
  if (workspace.guest_os !== "windows") return null;
  const state = workspace.windows_license;
  const license = state
    ? windowsActivationLabel(
        state.activation_status,
        state.activation_code ?? "",
        workspace.status === "running",
      )
    : null;
  return {
    os: "Windows",
    license,
    text: license ? `Windows · ${license.label}` : "Windows",
  };
}
