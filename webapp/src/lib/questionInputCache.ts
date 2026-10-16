import type { HarnessQuestionRequest } from '@/types/harness'

const CACHE_PREFIX = 'question-input:'

type Identity = {
  userId: string | number | null | undefined
  organizationId: string | null | undefined
}
type RequestScope = Pick<HarnessQuestionRequest, 'workspace_id' | 'session_id'>
type RequestId = RequestScope & Pick<HarnessQuestionRequest, 'request_id'>

export interface QuestionInputDraft {
  answers: string[]
  custom: string[]
}

function scopePrefix(identity: Identity, request: RequestScope): string {
  if (!identity.userId || !identity.organizationId || !request.workspace_id || !request.session_id)
    return ''
  return `${CACHE_PREFIX}${JSON.stringify([
    String(identity.userId),
    identity.organizationId,
    request.workspace_id,
    request.session_id,
  ])}:`
}

export function questionInputCacheKey(identity: Identity, request: RequestId): string {
  const prefix = scopePrefix(identity, request)
  return prefix && request.request_id ? `${prefix}${encodeURIComponent(request.request_id)}` : ''
}

/** Load a question draft from this tab, ignoring corrupt or unavailable storage. */
export function loadQuestionInputCache(
  key: string,
  questionCount: number,
): QuestionInputDraft | null {
  if (!key || typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(key)
    if (!raw) return null
    const draft: unknown = JSON.parse(raw)
    if (draft && typeof draft === 'object' && 'answers' in draft && 'custom' in draft) {
      const valid = (value: unknown): value is string[] =>
        Array.isArray(value) &&
        value.length === questionCount &&
        value.every((answer) => typeof answer === 'string')
      if (valid(draft.answers) && valid(draft.custom))
        return { answers: draft.answers, custom: draft.custom }
    }
    window.sessionStorage.removeItem(key)
  } catch {
    // Storage can be disabled or contain an invalid draft; keep the form usable.
  }
  return null
}

/** Save on each edit, rather than waiting for unmount or a debounced watcher. */
export function saveQuestionInputCache(key: string, draft: QuestionInputDraft): void {
  if (!key || typeof window === 'undefined') return
  try {
    if ([...draft.answers, ...draft.custom].some((value) => value.length > 0))
      window.sessionStorage.setItem(key, JSON.stringify(draft))
    else window.sessionStorage.removeItem(key)
  } catch {
    // Quota/private-mode failures must not interrupt editing or submission.
  }
}

function removeCaches(matches: (key: string) => boolean): void {
  if (typeof window === 'undefined') return
  try {
    for (let index = window.sessionStorage.length - 1; index >= 0; index--) {
      const key = window.sessionStorage.key(index)
      if (key?.startsWith(CACHE_PREFIX) && matches(key)) window.sessionStorage.removeItem(key)
    }
  } catch {
    // Storage may be unavailable; the form and authentication must still work.
  }
}

export function clearQuestionInputCache(identity: Identity, request: RequestId): void {
  const key = questionInputCacheKey(identity, request)
  if (key) removeCaches((candidate) => candidate === key)
}

/** Only an authoritative session snapshot may discard drafts for absent requests. */
export function pruneQuestionInputCaches(
  identity: Identity,
  session: RequestScope,
  pendingRequestIds: string[],
): void {
  const prefix = scopePrefix(identity, session)
  if (!prefix) return
  const pending = new Set(pendingRequestIds.map(encodeURIComponent))
  removeCaches((key) => key.startsWith(prefix) && !pending.has(key.slice(prefix.length)))
}

export function clearQuestionInputCaches(): void {
  removeCaches(() => true)
}
