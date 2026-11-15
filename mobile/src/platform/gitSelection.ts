import { CryptoDigestAlgorithm, digestStringAsync } from "expo-crypto";
import * as SecureStore from "expo-secure-store";
import type { SecureStoreAdapter } from "./storage";

/** Last repository path per server/organization/workspace; no Git contents. */
export class NativeGitSelectionStorage {
  private queue: Promise<void> = Promise.resolve();

  constructor(private readonly store: SecureStoreAdapter = SecureStore) {}

  load(scope: string): Promise<string | null> {
    return this.enqueue(async () =>
      (await this.store.getItemAsync(await this.key(scope))) || null,
    );
  }

  save(scope: string, path: string): Promise<void> {
    return this.enqueue(async () => {
      await this.store.setItemAsync(await this.key(scope), path);
    });
  }

  private async key(scope: string): Promise<string> {
    const digest = await digestStringAsync(CryptoDigestAlgorithm.SHA256, scope);
    return `opencuria.git-selection.v1.${digest}`;
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.queue.then(operation, operation);
    this.queue = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}
