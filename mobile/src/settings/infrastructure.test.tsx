import React from "react";
import { Text } from "react-native";
import { act, render, screen, waitFor } from "@testing-library/react-native";
import {
  avatarColor,
  isOrganizationAdmin,
  profileInitials,
  profileName,
} from "./profile";
import { ResourceStore, useResource } from "./resources";
import { useAutosave, autosaveText } from "./useAutosave";

jest.mock("../ui/toast", () => ({ showToast: jest.fn() }));

describe("profile", () => {
  it("names and abbreviates the account like ChatGPT's avatar", () => {
    const ada = {
      first_name: "Ada",
      last_name: "Lovelace",
      email: "ada@example.com",
    };
    expect(profileName(ada)).toBe("Ada Lovelace");
    expect(profileInitials(ada)).toBe("AL");
    const anonymous = {
      first_name: "",
      last_name: "",
      email: "grace.hopper@navy.mil",
    };
    expect(profileName(anonymous)).toBe("grace.hopper@navy.mil");
    expect(profileInitials(anonymous)).toBe("GH");
    expect(profileInitials({ ...anonymous, email: "x@y.z" })).toBe("X");
    expect(avatarColor(ada)).toBe(avatarColor({ email: "ada@example.com" }));
  });

  it("derives admin from the active organization's role", () => {
    const organizations = [
      { id: "a", name: "A", slug: "a", role: "admin", created_at: "" },
      { id: "b", name: "B", slug: "b", role: "member", created_at: "" },
    ];
    expect(isOrganizationAdmin(organizations, "a")).toBe(true);
    expect(isOrganizationAdmin(organizations, "b")).toBe(false);
    expect(isOrganizationAdmin(organizations, null)).toBe(false);
  });
});

describe("resources", () => {
  it("shares one load between pages and lets a save win over a slower load", async () => {
    const store = new ResourceStore();
    let finish!: (value: string) => void;
    const loader = jest.fn(
      () => new Promise<string>((resolve) => (finish = resolve)),
    );
    void store.load("skills", loader);
    void store.load("skills", loader);
    expect(loader).toHaveBeenCalledTimes(1);
    store.set("skills", "saved");
    await act(async () => finish("stale"));
    expect(store.get("skills").data).toBe("saved");
  });

  it("reloads mounted pages after an invalidation", async () => {
    const store = new ResourceStore();
    let version = 0;
    function Page() {
      const resource = useResource(
        store,
        "credentials:list",
        async () => ++version,
      );
      return <Text>{`version ${resource.data ?? 0}`}</Text>;
    }
    render(<Page />);
    expect(await screen.findByText("version 1")).toBeTruthy();
    act(() => store.invalidate("credentials"));
    expect(await screen.findByText("version 2")).toBeTruthy();
  });
});

describe("autosave", () => {
  function Form({
    value,
    save,
    valid = true,
  }: {
    value: number;
    save: (value: number) => Promise<void>;
    valid?: boolean;
  }) {
    const autosave = useAutosave(value, save, { valid, delay: 10 });
    return <Text>{autosaveText(autosave)}</Text>;
  }

  it("saves edits after the baseline, skips invalid values and retries failures", async () => {
    const save = jest
      .fn<Promise<void>, [number]>()
      .mockRejectedValueOnce(new Error("Offline"))
      .mockResolvedValue(undefined);
    const view = render(<Form value={1} save={save} />);
    expect(screen.getByText("All changes saved")).toBeTruthy();
    view.rerender(<Form value={2} save={save} valid={false} />);
    expect(screen.getByText("Check your input")).toBeTruthy();
    view.rerender(<Form value={3} save={save} />);
    expect(await screen.findByText("Changes not saved: Offline")).toBeTruthy();
    expect(save).toHaveBeenCalledWith(3);
    view.rerender(<Form value={4} save={save} />);
    // A failed save waits for an explicit retry instead of looping.
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("writes a pending edit when the page closes", async () => {
    const save = jest.fn(async () => undefined);
    const view = render(<Form value={1} save={save} />);
    view.rerender(<Form value={5} save={save} />);
    view.unmount();
    await waitFor(() => expect(save).toHaveBeenCalledWith(5));
  });
});
