import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { AppState } from "react-native";
import {
  chatInputCacheKey,
  createChatInputCache,
  type ChatInputScope,
} from "@opencuria/chat-core/chatInputCache";
import { ChatDraftStore, type DraftAccount } from "./chatDrafts";

type DraftUpdate = string | ((current: string) => string);
/** Whether the draft goes along with a change from one chat to another. */
export type DraftFollowRule = (
  from: ChatInputScope,
  to: ChatInputScope,
) => boolean;

/**
 * The composer draft of the chat on screen (`scope`), as in the webapp: each
 * chat, each workspace's new chat and each engine keeps its own draft, which
 * comes back when that chat shows again. Without a scope nothing is saved.
 */
export function useChatDraft(
  account: DraftAccount | null,
  scope: ChatInputScope | null,
): {
  draft: string;
  /** An edit of the draft on screen; it is saved for its chat. */
  setDraft: (update: DraftUpdate) => void;
  /**
   * The draft on screen moves to the next chat if `rule` allows it, instead
   * of showing that chat's own draft. Returns a function that withdraws the
   * rule; `null` withdraws any rule.
   */
  followNext: (rule: DraftFollowRule | null) => () => void;
} {
  const store = useMemo(() => new ChatDraftStore(account), [account]);
  const cache = useMemo(() => createChatInputCache(() => store), [store]);
  const key = scope ? chatInputCacheKey(scope) : null;
  const [draft, setShownDraft] = useState(() =>
    scope ? cache.load(scope) : "",
  );
  // The chat whose draft is on screen; it changes with the layout effect.
  const shown = useRef({ key, scope, text: draft });
  const cacheRef = useRef(cache);
  cacheRef.current = cache;
  const followRule = useRef<DraftFollowRule | null>(null);

  const show = useCallback((text: string) => {
    shown.current.text = text;
    setShownDraft(text);
  }, []);

  useLayoutEffect(() => {
    const previous = shown.current;
    if (previous.key === key) return;
    const rule = followRule.current;
    followRule.current = null;
    shown.current = { key, scope, text: previous.text };
    if (previous.scope && scope && rule?.(previous.scope, scope)) {
      cache.move(previous.scope, scope, previous.text);
      return;
    }
    show(scope ? cache.load(scope) : "");
    // `key` identifies `scope`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, cache, show]);

  useEffect(() => {
    let alive = true;
    // Drafts saved on the device arrive after the first render; typing wins.
    void store.ready.then(() => {
      const { scope: current, text } = shown.current;
      const saved = alive && current && !text && cacheRef.current.load(current);
      if (saved) show(saved);
    });
    const background = AppState.addEventListener("change", (state) => {
      if (state !== "active") void store.flush();
    });
    return () => {
      alive = false;
      background.remove();
      void store.flush();
    };
  }, [store, show]);

  const setDraft = useCallback(
    (update: DraftUpdate) => {
      const { scope: current, text } = shown.current;
      const next = typeof update === "function" ? update(text) : update;
      if (next === text) return;
      show(next);
      if (current) cacheRef.current.save(current, next);
    },
    [show],
  );

  const followNext = useCallback((rule: DraftFollowRule | null) => {
    followRule.current = rule;
    return () => {
      if (rule && followRule.current === rule) followRule.current = null;
    };
  }, []);

  return { draft, setDraft, followNext };
}
