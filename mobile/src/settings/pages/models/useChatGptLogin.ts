import { useCallback, useEffect, useRef, useState } from "react";
import {
  chatGptOAuthOutcome,
  type ProviderAccountInput,
} from "@opencuria/chat-core/providerAccounts";
import type { OpenCuriaClient } from "@opencuria/client";
import { errorMessage } from "../../resources";

export interface ChatGptLogin {
  /** A device code is shown and the server is polled. */
  pending: boolean;
  starting: boolean;
  userCode: string;
  verificationUrl: string;
  error: string | null;
  start: (
    input: Pick<ProviderAccountInput, "label" | "shared"> & {
      account_id?: string;
    },
  ) => Promise<void>;
  cancel: () => void;
}

/**
 * ChatGPT device login: shows a code to enter at OpenAI and polls the
 * server, one request at a time, until it connects, expires or is denied.
 * A running flow is cancelled when a new one starts and on unmount.
 */
export function useChatGptLogin(
  client: OpenCuriaClient,
  onConnected: () => void,
): ChatGptLogin {
  const [state, setState] = useState({
    pending: false,
    starting: false,
    userCode: "",
    verificationUrl: "",
    error: null as string | null,
  });
  const flow = useRef<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const generation = useRef(0);
  const connected = useRef(onConnected);
  connected.current = onConnected;

  const stop = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    const id = flow.current;
    flow.current = null;
    generation.current += 1;
    if (id)
      void client.providerAccounts
        .cancelChatGptOAuth(id)
        .catch(() => undefined);
  }, [client]);

  const poll = useCallback(
    async (id: string, seconds: number, owner: number) => {
      try {
        const status = await client.providerAccounts.chatGptOAuthStatus(id);
        if (owner !== generation.current) return;
        const outcome = chatGptOAuthOutcome(status.status);
        if (outcome === "pending") {
          timer.current = setTimeout(
            () => void poll(id, seconds, owner),
            seconds * 1000,
          );
          return;
        }
        flow.current = null;
        setState((current) => ({
          ...current,
          pending: false,
          error: typeof outcome === "object" ? outcome.error : null,
        }));
        if (outcome === "connected") connected.current();
      } catch (cause) {
        if (owner !== generation.current) return;
        flow.current = null;
        setState((current) => ({
          ...current,
          pending: false,
          error: errorMessage(cause, "OAuth polling failed"),
        }));
      }
    },
    [client],
  );

  const start = useCallback<ChatGptLogin["start"]>(
    async (input) => {
      stop();
      const owner = generation.current;
      setState({
        pending: false,
        starting: true,
        userCode: "",
        verificationUrl: "",
        error: null,
      });
      try {
        const started = await client.providerAccounts.startChatGptOAuth(input);
        if (owner !== generation.current) {
          void client.providerAccounts
            .cancelChatGptOAuth(started.flow_id)
            .catch(() => undefined);
          return;
        }
        if (!started.flow_id)
          throw new Error("OAuth flow could not be scoped to this account.");
        flow.current = started.flow_id;
        const seconds = Math.max(1, started.interval || 5);
        setState({
          pending: true,
          starting: false,
          userCode: started.user_code,
          verificationUrl: started.verification_url,
          error: null,
        });
        timer.current = setTimeout(
          () => void poll(started.flow_id, seconds, owner),
          seconds * 1000,
        );
      } catch (cause) {
        if (owner !== generation.current) return;
        setState((current) => ({
          ...current,
          starting: false,
          error: errorMessage(cause, "Failed to start OAuth"),
        }));
      }
    },
    [client, poll, stop],
  );

  const cancel = useCallback(() => {
    stop();
    setState((current) => ({ ...current, pending: false, starting: false }));
  }, [stop]);

  useEffect(() => stop, [stop]);
  return { ...state, start, cancel };
}
