import { act, renderHook, waitFor } from "@testing-library/react-native";
import { AppState } from "react-native";
import type { ChatInputScope } from "@opencuria/chat-core/chatInputCache";
import type { SavedDraft } from "../offline/offlineStore";
import { useChatDraft } from "./useChatDraft";

function account(saved: SavedDraft[] = []) {
  let release: () => void = () => undefined;
  const loaded = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    release,
    loadDrafts: jest.fn(async () => {
      await loaded;
      return saved;
    }),
    saveDrafts: jest.fn(async (_drafts: SavedDraft[]) => undefined),
  };
}

const newChat: ChatInputScope = { workspaceId: "w1" };
const chat: ChatInputScope = { workspaceId: "w1", sessionId: "s1" };

function renderDraft(
  device: ReturnType<typeof account>,
  scope: ChatInputScope | null,
) {
  return renderHook(
    ({ current }: { current: ChatInputScope | null }) =>
      useChatDraft(device, current),
    { initialProps: { current: scope } },
  );
}

it("shows each chat's own draft again", async () => {
  const device = account();
  device.release();
  const view = renderDraft(device, newChat);

  act(() => view.result.current.setDraft("idea"));
  view.rerender({ current: chat });
  expect(view.result.current.draft).toBe("");
  act(() => view.result.current.setDraft((text) => `${text}reply`));

  view.rerender({ current: newChat });
  expect(view.result.current.draft).toBe("idea");
  view.rerender({ current: { ...chat, harnessId: "claude" } });
  expect(view.result.current.draft).toBe("");
  view.rerender({ current: chat });
  expect(view.result.current.draft).toBe("reply");
});

it("moves the draft along with the next chat a rule allows", async () => {
  const device = account();
  device.release();
  const view = renderDraft(device, newChat);
  act(() => view.result.current.setDraft("carried"));

  act(() => {
    view.result.current.followNext((_from, to) => to.harnessId === "claude");
  });
  const claude = { ...newChat, harnessId: "claude" as const };
  view.rerender({ current: claude });
  expect(view.result.current.draft).toBe("carried");

  // The rule applied once; the draft left the native new chat.
  view.rerender({ current: newChat });
  expect(view.result.current.draft).toBe("");
  view.rerender({ current: claude });
  expect(view.result.current.draft).toBe("carried");

  let withdraw = () => undefined as void;
  act(() => {
    withdraw = view.result.current.followNext(() => true);
  });
  withdraw();
  view.rerender({ current: chat });
  expect(view.result.current.draft).toBe("");
});

it("shows saved drafts once loaded, unless something was typed", async () => {
  const saved = [
    {
      key: "chat-input-w1-default",
      text: "saved idea",
      editedAt: new Date().toISOString(),
    },
    {
      key: "chat-input-w1-s1",
      text: "saved reply",
      editedAt: new Date().toISOString(),
    },
  ];
  const quiet = account(saved);
  const view = renderDraft(quiet, newChat);
  expect(view.result.current.draft).toBe("");
  await act(async () => quiet.release());
  expect(view.result.current.draft).toBe("saved idea");

  const busy = account(saved);
  const typing = renderDraft(busy, chat);
  act(() => typing.result.current.setDraft("typed first"));
  await act(async () => busy.release());
  expect(typing.result.current.draft).toBe("typed first");
});

it("saves when the app leaves the foreground and on unmount", async () => {
  const listeners: Array<(state: string) => void> = [];
  jest
    .spyOn(AppState, "addEventListener")
    .mockImplementation((_type, listener) => {
      listeners.push(listener as (state: string) => void);
      return { remove: jest.fn() } as never;
    });
  const device = account();
  device.release();
  const view = renderDraft(device, chat);
  act(() => view.result.current.setDraft("almost done"));

  act(() => listeners.forEach((listener) => listener("background")));
  await waitFor(() =>
    expect(device.saveDrafts).toHaveBeenCalledWith([
      expect.objectContaining({
        key: "chat-input-w1-s1",
        text: "almost done",
      }),
    ]),
  );

  act(() => view.result.current.setDraft(""));
  view.unmount();
  await waitFor(() => expect(device.saveDrafts).toHaveBeenLastCalledWith([]));
});

it("keeps nothing without a chat", () => {
  const device = account();
  device.release();
  const view = renderDraft(device, null);
  act(() => view.result.current.setDraft("loose text"));
  expect(view.result.current.draft).toBe("loose text");
  view.rerender({ current: newChat });
  expect(view.result.current.draft).toBe("");
});

it("adds to the draft of a chat that is not on screen without hiding its saved text", async () => {
  const device = account([
    {
      key: "chat-input-w1-s1",
      text: "Saved reply",
      editedAt: new Date().toISOString(),
    },
  ]);
  const view = renderDraft(device, newChat);
  let edited!: Promise<void>;
  act(() => {
    edited = view.result.current.editDraft(
      chat,
      (text) => `${text}\nDictated.`,
    );
  });
  device.release();
  await act(() => edited);
  expect(view.result.current.draft).toBe("");
  expect(device.saveDrafts).toHaveBeenLastCalledWith([
    expect.objectContaining({
      key: "chat-input-w1-s1",
      text: "Saved reply\nDictated.",
    }),
  ]);
  view.rerender({ current: chat });
  expect(view.result.current.draft).toBe("Saved reply\nDictated.");

  // The chat on screen changes as if typed.
  await act(() =>
    view.result.current.editDraft(chat, (text) => `${text} More.`),
  );
  expect(view.result.current.draft).toBe("Saved reply\nDictated. More.");
});
