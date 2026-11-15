import type { AgentActivitySnapshot } from "@opencuria/client";

export interface LiveActivityHandle {
  id: string;
  update(snapshot: AgentActivitySnapshot): Promise<void>;
  end(snapshot?: AgentActivitySnapshot): Promise<void>;
  token(): Promise<string | null>;
  onToken(listener: (token: string) => void): () => void;
}

export interface LiveActivities {
  available: boolean;
  instances(): Promise<LiveActivityHandle[]>;
  start(snapshot: AgentActivitySnapshot): Promise<LiveActivityHandle>;
}

/** Android has native notifications; Live Activities are an iOS feature. */
export const liveActivities: LiveActivities = {
  available: false,
  instances: async () => [],
  start: async () => {
    throw new Error("Live Activities require iOS.");
  },
};
