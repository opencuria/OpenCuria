import { computed, ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'

/** Persist collapsed workspace IDs independently for each authenticated user/org scope. */
export function useSidebarWorkspaceCollapse(scope: MaybeRefOrGetter<string>): {
  collapsedWorkspaceIds: Ref<string[]>
  setCollapsed: (workspaceId: string, collapsed: boolean) => void
} {
  const collapsedWorkspaceIds = ref<string[]>([])
  const storageKey = computed(() => {
    const resolvedScope = toValue(scope)
    return resolvedScope ? `opencuria-sidebar-collapsed-workspaces:${resolvedScope}` : ''
  })

  watch(
    storageKey,
    (key) => {
      collapsedWorkspaceIds.value = []
      if (!key || typeof window === 'undefined') return
      try {
        const raw = window.localStorage.getItem(key)
        if (raw == null) return
        const parsed: unknown = JSON.parse(raw)
        if (
          Array.isArray(parsed) &&
          parsed.every((id) => typeof id === 'string' && id.trim().length > 0)
        ) {
          collapsedWorkspaceIds.value = [...new Set<string>(parsed)]
        }
      } catch {
        // Ignore corrupt data / disabled storage and keep the expanded default.
      }
    },
    { immediate: true, flush: 'sync' },
  )

  function setCollapsed(workspaceId: string, collapsed: boolean): void {
    if (!workspaceId.trim()) return
    const ids = new Set(collapsedWorkspaceIds.value)
    if (collapsed) ids.add(workspaceId)
    else ids.delete(workspaceId)
    collapsedWorkspaceIds.value = [...ids]

    const key = storageKey.value
    if (!key || typeof window === 'undefined') return
    try {
      window.localStorage.setItem(key, JSON.stringify(collapsedWorkspaceIds.value))
    } catch {
      // Ignore quota / private-mode failures; the in-memory state is already updated.
    }
  }

  return { collapsedWorkspaceIds, setCollapsed }
}
