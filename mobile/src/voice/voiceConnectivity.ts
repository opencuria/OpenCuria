import { AppState } from "react-native";
import type { VoiceConnectivity } from "@opencuria/chat-core/voiceTranscriptions";
import { watchNetwork } from "../offline/connectivity";

/**
 * Whether waiting recordings can reach OpenCuria: the device has a network
 * and the session reaches the server. Listeners hear `true` when either
 * comes back and when the app returns to the foreground, where iOS may have
 * held requests back.
 */
export class AppVoiceConnectivity implements VoiceConnectivity {
  private network = true;
  private server = true;
  private readonly listeners = new Set<(online: boolean) => void>();
  private readonly stops: Array<() => void> = [];

  constructor() {
    this.stops.push(
      watchNetwork((connected) =>
        this.change(() => (this.network = connected)),
      ),
    );
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && this.online()) this.emit(true);
    });
    this.stops.push(() => subscription.remove());
  }

  online(): boolean {
    return this.network && this.server;
  }

  /** The session found OpenCuria (un)reachable. */
  setServerReachable(reachable: boolean): void {
    this.change(() => (this.server = reachable));
  }

  watch(listener: (online: boolean) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  dispose(): void {
    for (const stop of this.stops.splice(0))
      try {
        stop();
      } catch {
        // A platform listener that is already gone.
      }
    this.listeners.clear();
  }

  private change(update: () => void): void {
    const before = this.online();
    update();
    if (this.online() !== before) this.emit(this.online());
  }

  private emit(online: boolean): void {
    for (const listener of [...this.listeners]) listener(online);
  }
}
