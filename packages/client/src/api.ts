import type { TokenPair } from "./types";

export type FetchLike = typeof fetch;

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string;
  readonly data: unknown;

  constructor(status: number, detail: string, code = "error", data?: unknown) {
    super(detail);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

export interface ApiClientOptions {
  apiBaseUrl: string;
  getAccessToken: () => string | null;
  getRefreshToken: () => string | null;
  setTokens: (tokens: TokenPair) => Promise<void> | void;
  getOrganizationId: () => string | null;
  onAuthenticationExpired: () => void;
  fetch?: FetchLike;
  /** Ambient cookie policy: browser clients use same-origin; native clients omit cookies. */
  credentials?: RequestCredentials;
}

export interface ApiContext {
  baseUrl: string;
  organizationId: string | null;
  accessToken: string | null;
  refreshToken: string | null;
  generation: number;
}

export interface ApiResponse<T> {
  ok: boolean;
  status: number;
  data: T;
}

interface RefreshSuccess {
  kind: "refreshed";
  tokens: TokenPair;
}

interface RefreshRejected {
  kind: "rejected";
  status: number;
}

interface RefreshUnavailable {
  kind: "unavailable";
}

type RefreshOutcome = RefreshSuccess | RefreshRejected | RefreshUnavailable;

const AUTH_ENDPOINT_PREFIX = "/auth/";

function normalizeBaseUrl(value: string): string {
  return value.replace(/\/+$/, "");
}

function safeApiPath(path: string): string {
  // API methods intentionally accept paths relative to the configured API root.
  // Never allow caller-provided absolute/protocol-relative URLs to receive auth.
  if (
    !path.startsWith("/") ||
    path.startsWith("//") ||
    /^[a-z][a-z\d+.-]*:/i.test(path)
  ) {
    throw new ApiRequestError(
      0,
      "API path must be a same-origin absolute path",
      "invalid_path",
    );
  }
  return path;
}

function errorPayload(data: unknown): { detail: string; code: string } {
  const payload =
    data && typeof data === "object" ? (data as Record<string, unknown>) : {};
  const detail =
    typeof payload.detail === "string"
      ? payload.detail
      : typeof payload.message === "string"
        ? payload.message
        : "Unknown error";
  const code =
    typeof payload.code === "string" && payload.code ? payload.code : "error";
  return { detail, code };
}

/** Framework-neutral REST client with context-safe single-flight token refresh. */
export class ApiClient {
  private readonly fetchImpl: FetchLike;
  private generation = 0;
  private observedContext = "";
  private contextListeners = new Set<() => void>();
  private refreshFlights = new Map<string, Promise<RefreshOutcome>>();
  private rejectedRefreshes = new Set<string>();
  private expiredGenerations = new Set<number>();

  constructor(private readonly options: ApiClientOptions) {
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.request = this.request.bind(this);
    this.requestWithStatus = this.requestWithStatus.bind(this);
    this.get = this.get.bind(this);
    this.post = this.post.bind(this);
    this.put = this.put.bind(this);
    this.patch = this.patch.bind(this);
    this.delete = this.delete.bind(this);
  }

  /** Current auth/org/base context; also advances generation on external context changes. */
  getContext(): ApiContext {
    const baseUrl = normalizeBaseUrl(this.options.apiBaseUrl);
    const organizationId = this.options.getOrganizationId();
    const accessToken = this.options.getAccessToken();
    const refreshToken = this.options.getRefreshToken();
    const hasAuth = Boolean(accessToken || refreshToken);
    const key = JSON.stringify([baseUrl, organizationId, hasAuth]);
    const changed =
      Boolean(this.observedContext) && this.observedContext !== key;
    if (changed) this.generation += 1;
    this.observedContext = key;
    if (changed) {
      for (const listener of [...this.contextListeners]) listener();
    }
    return {
      baseUrl,
      organizationId,
      accessToken,
      refreshToken,
      generation: this.generation,
    };
  }

  /** Request with the client's cookie policy, overridable for OAuth actions. */
  request<T>(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
    credentials: RequestCredentials = this.options.credentials ?? "omit",
  ): Promise<T> {
    return this.performRequest<T>(method, path, body, credentials, signal).then(
      (result) => {
        if (!result.ok) {
          const error = errorPayload(result.data);
          throw new ApiRequestError(
            result.status,
            error.detail,
            error.code,
            result.data,
          );
        }
        return result.data;
      },
    );
  }

  /** Execute with the same auth/retry behavior but preserve non-2xx payloads. */
  requestWithStatus<T>(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
    credentials: RequestCredentials = this.options.credentials ?? "omit",
  ): Promise<ApiResponse<T>> {
    return this.performRequest<T>(
      method,
      path,
      body,
      credentials,
      signal,
      true,
    );
  }

  get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>("GET", path, undefined, signal);
  }

  post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>("POST", path, body, signal);
  }

  put<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>("PUT", path, body, signal);
  }

  patch<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>("PATCH", path, body, signal);
  }

  delete<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>("DELETE", path, undefined, signal);
  }

  /** Call after a caller changes tokens outside this client's refresh/login flows. */
  notifyTokensChanged(): void {
    this.generation += 1;
    for (const listener of [...this.contextListeners]) listener();
  }

  /** Notify dependent transports when logout, org, API base, or explicit token context changes. */
  onContextChange(callback: () => void): () => void {
    this.contextListeners.add(callback);
    return () => this.contextListeners.delete(callback);
  }

  /** Share the same safe refresh flow with realtime Socket.IO auth recovery. */
  async refreshAccessToken(): Promise<boolean> {
    const context = this.getContext();
    const outcome = await this.refresh(context);
    if (outcome.kind === "rejected") this.expireIfCurrent(context);
    return outcome.kind === "refreshed";
  }

  private async performRequest<T>(
    method: string,
    path: string,
    body: unknown,
    credentials: RequestCredentials,
    signal?: AbortSignal,
    preserveHttpErrors = false,
  ): Promise<ApiResponse<T>> {
    const apiPath = safeApiPath(path);
    const normalizedMethod = method.toUpperCase();
    const authEndpoint = apiPath.startsWith(AUTH_ENDPOINT_PREFIX);
    const initialContext = this.getContext();
    const initialResponse = await this.send(
      normalizedMethod,
      apiPath,
      body,
      signal,
      initialContext,
      credentials,
    );
    if (initialResponse.status !== 401 || authEndpoint) {
      return this.toApiResponse<T>(initialResponse);
    }

    const current = this.getContext();
    // Another concurrent request may already have refreshed the token. Retry this
    // original request once rather than starting a second refresh flight.
    if (
      this.sameGenerationScope(initialContext, current) &&
      initialContext.accessToken !== current.accessToken
    ) {
      const retry = await this.send(
        normalizedMethod,
        apiPath,
        body,
        signal,
        current,
        credentials,
      );
      return this.toApiResponse<T>(retry);
    }
    if (!this.sameGenerationScope(initialContext, current)) {
      return this.toApiResponse<T>(initialResponse);
    }

    const outcome = await this.refresh(initialContext);
    if (preserveHttpErrors && outcome.kind !== "refreshed") {
      return this.toApiResponse<T>(initialResponse);
    }
    if (outcome.kind === "refreshed") {
      const afterRefresh = this.getContext();
      if (!this.sameGenerationScope(initialContext, afterRefresh)) {
        return this.toApiResponse<T>(initialResponse);
      }
      const retry = await this.send(
        normalizedMethod,
        apiPath,
        body,
        signal,
        afterRefresh,
        credentials,
        outcome.tokens.access_token,
      );
      // A protected endpoint's second 401 is an error, but is not authority
      // to clear a freshly rotated session. Only refresh 401/403 expires auth.
      return this.toApiResponse<T>(retry);
    }

    if (outcome.kind === "rejected") this.expireIfCurrent(initialContext);
    return this.toApiResponse<T>(initialResponse);
  }

  private sameGenerationScope(left: ApiContext, right: ApiContext): boolean {
    return (
      left.generation === right.generation &&
      left.baseUrl === right.baseUrl &&
      left.organizationId === right.organizationId
    );
  }

  private sameScope(left: ApiContext, right: ApiContext): boolean {
    return (
      this.sameGenerationScope(left, right) &&
      left.accessToken === right.accessToken &&
      left.refreshToken === right.refreshToken
    );
  }

  private async refresh(context: ApiContext): Promise<RefreshOutcome> {
    if (!context.refreshToken) return { kind: "unavailable" };
    const key = `${context.generation}:${context.baseUrl}:${context.organizationId ?? ""}:${context.refreshToken}`;
    const existing = this.refreshFlights.get(key);
    if (existing) return existing;
    if (this.rejectedRefreshes.has(key))
      return { kind: "rejected", status: 401 };

    const flight = (async (): Promise<RefreshOutcome> => {
      try {
        const beforeRefresh = this.getContext();
        if (!this.sameScope(context, beforeRefresh))
          return { kind: "unavailable" };
        const response = await this.fetchImpl(
          `${context.baseUrl}/auth/refresh/`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: context.refreshToken }),
            credentials: this.options.credentials ?? "omit",
          },
        );
        if (response.status === 401 || response.status === 403) {
          this.rejectedRefreshes.add(key);
          return { kind: "rejected", status: response.status };
        }
        if (!response.ok) return { kind: "unavailable" };
        const tokens = (await response.json()) as TokenPair;
        if (!tokens?.access_token || !tokens?.refresh_token)
          return { kind: "unavailable" };
        const current = this.getContext();
        if (!this.sameScope(context, current)) return { kind: "unavailable" };

        const beforeSet = this.getContext();
        if (!this.sameScope(context, beforeSet)) return { kind: "unavailable" };
        await this.options.setTokens(tokens);
        const afterSet = this.getContext();
        if (
          afterSet.baseUrl !== context.baseUrl ||
          afterSet.organizationId !== context.organizationId ||
          afterSet.refreshToken !== tokens.refresh_token
        ) {
          return { kind: "unavailable" };
        }
        return { kind: "refreshed", tokens };
      } catch {
        return { kind: "unavailable" };
      }
    })();
    this.refreshFlights.set(key, flight);
    try {
      return await flight;
    } finally {
      if (this.refreshFlights.get(key) === flight)
        this.refreshFlights.delete(key);
      const current = this.getContext();
      const currentKey = `${current.generation}:${current.baseUrl}:${current.organizationId ?? ""}:${current.refreshToken ?? ""}`;
      for (const rejectedKey of this.rejectedRefreshes) {
        if (rejectedKey !== currentKey)
          this.rejectedRefreshes.delete(rejectedKey);
      }
    }
  }

  private expireIfCurrent(context: ApiContext): void {
    const current = this.getContext();
    if (
      !this.sameScope(context, current) ||
      this.expiredGenerations.has(context.generation)
    )
      return;
    this.expiredGenerations.add(context.generation);
    this.options.onAuthenticationExpired();
  }

  private async send(
    method: string,
    path: string,
    body: unknown,
    signal: AbortSignal | undefined,
    context: ApiContext,
    credentials: RequestCredentials,
    accessToken = context.accessToken,
  ): Promise<Response> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    if (context.organizationId)
      headers["X-Organization-Id"] = context.organizationId;
    return this.fetchImpl(`${context.baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials,
      signal,
    });
  }

  private async toApiResponse<T>(response: Response): Promise<ApiResponse<T>> {
    if (response.status === 204) {
      return { ok: response.ok, status: response.status, data: undefined as T };
    }
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      data = undefined;
    }
    return { ok: response.ok, status: response.status, data: data as T };
  }
}
