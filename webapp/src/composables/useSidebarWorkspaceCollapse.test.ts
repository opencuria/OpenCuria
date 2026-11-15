import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, type MaybeRefOrGetter } from 'vue'

import { useSidebarWorkspaceCollapse } from './useSidebarWorkspaceCollapse'

const key = (scope: string) => `opencuria-sidebar-collapsed-workspaces:${scope}`
const scopes: ReturnType<typeof effectScope>[] = []

function mount(scope: MaybeRefOrGetter<string> = 'user-a:org-a') {
  const instanceScope = effectScope()
  scopes.push(instanceScope)
  return instanceScope.run(() => useSidebarWorkspaceCollapse(scope))!
}

describe('useSidebarWorkspaceCollapse', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    scopes.splice(0).forEach((scope) => scope.stop())
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('defaults to expanded and persists explicit collapse/expand actions for fresh instances', () => {
    const state = mount()
    expect(state.collapsedWorkspaceIds.value).toEqual([])
    state.setCollapsed('workspace-a', true)
    state.setCollapsed('workspace-a', true)
    state.setCollapsed('workspace-b', true)
    expect(state.collapsedWorkspaceIds.value).toEqual(['workspace-a', 'workspace-b'])
    expect(localStorage.getItem(key('user-a:org-a'))).toBe('["workspace-a","workspace-b"]')
    expect(mount().collapsedWorkspaceIds.value).toEqual(['workspace-a', 'workspace-b'])
    state.setCollapsed('workspace-a', false)
    expect(state.collapsedWorkspaceIds.value).toEqual(['workspace-b'])
    expect(mount().collapsedWorkspaceIds.value).toEqual(['workspace-b'])
  })

  it('loads each user/org synchronously on switching away and back without bleeding IDs', () => {
    const identity = ref('user-a:org-a')
    const state = mount(() => identity.value)
    state.setCollapsed('a', true)
    identity.value = 'user-a:org-b'
    expect(state.collapsedWorkspaceIds.value).toEqual([])
    state.setCollapsed('b', true)
    identity.value = 'user-b:org-b'
    expect(state.collapsedWorkspaceIds.value).toEqual([])
    state.setCollapsed('c', true)
    identity.value = 'user-a:org-a'
    expect(state.collapsedWorkspaceIds.value).toEqual(['a'])
    identity.value = 'user-a:org-b'
    expect(state.collapsedWorkspaceIds.value).toEqual(['b'])
    expect(mount('user-b:org-b').collapsedWorkspaceIds.value).toEqual(['c'])
    expect(localStorage.getItem(key('user-a:org-a'))).toBe('["a"]')
  })

  it('retains IDs for absent or stopped workspaces when other IDs change', () => {
    localStorage.setItem(key('user-a:org-a'), '["temporarily-absent","stopped"]')
    const state = mount()
    state.setCollapsed('active', true)
    state.setCollapsed('active', false)
    expect(mount().collapsedWorkspaceIds.value).toEqual(['temporarily-absent', 'stopped'])
  })

  it('deduplicates valid persisted IDs', () => {
    localStorage.setItem(key('user-a:org-a'), '["a","a","b"]')
    expect(mount().collapsedWorkspaceIds.value).toEqual(['a', 'b'])
  })

  it.each([
    'not-json',
    'null',
    '{}',
    '"a"',
    '42',
    '["a",null]',
    '["a",1]',
    '["a",{}]',
    '["a",""]',
    '["a"," "]',
  ])('safely rejects the entire invalid stored value %s', (raw) => {
    localStorage.setItem(key('user-a:org-a'), raw)
    expect(mount().collapsedWorkspaceIds.value).toEqual([])
  })

  it('does not access persistent storage for an empty scope, including explicit actions', () => {
    const get = vi.spyOn(Storage.prototype, 'getItem')
    const set = vi.spyOn(Storage.prototype, 'setItem')
    const identity = ref('')
    const state = mount(identity)
    state.setCollapsed('anonymous', true)
    expect(state.collapsedWorkspaceIds.value).toEqual(['anonymous'])
    expect(get).not.toHaveBeenCalled()
    expect(set).not.toHaveBeenCalled()
    identity.value = 'user-a:org-a'
    expect(state.collapsedWorkspaceIds.value).toEqual([])
    state.setCollapsed('authenticated', true)
    identity.value = ''
    expect(state.collapsedWorkspaceIds.value).toEqual([])
    state.setCollapsed('anonymous', true)
    identity.value = 'user-a:org-a'
    expect(state.collapsedWorkspaceIds.value).toEqual(['authenticated'])
  })

  it('keeps functioning when reading or writing storage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('disabled')
    })
    const state = mount()
    expect(state.collapsedWorkspaceIds.value).toEqual([])
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      expect(state.collapsedWorkspaceIds.value).toEqual(['a'])
      throw new Error('quota')
    })
    expect(() => state.setCollapsed('a', true)).not.toThrow()
    expect(state.collapsedWorkspaceIds.value).toEqual(['a'])
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('disabled')
    })
    expect(() => state.setCollapsed('a', false)).not.toThrow()
    expect(state.collapsedWorkspaceIds.value).toEqual([])
  })

  it('keeps functioning when the storage getter itself throws', () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new Error('security')
    })
    const state = mount()
    expect(() => state.setCollapsed('a', true)).not.toThrow()
    expect(state.collapsedWorkspaceIds.value).toEqual(['a'])
  })

  it('supports SSR without window', () => {
    vi.stubGlobal('window', undefined)
    const state = mount()
    expect(state.collapsedWorkspaceIds.value).toEqual([])
    state.setCollapsed('a', true)
    expect(state.collapsedWorkspaceIds.value).toEqual(['a'])
  })

  it('writes only through explicit actions, not loading, switching or ref changes', async () => {
    localStorage.setItem(key('user-a:org-a'), '["a"]')
    const set = vi.spyOn(Storage.prototype, 'setItem')
    const identity = ref('user-a:org-a')
    const state = mount(identity)
    state.collapsedWorkspaceIds.value.push('local-only')
    await nextTick()
    identity.value = 'user-a:org-b'
    await nextTick()
    identity.value = 'user-a:org-a'
    expect(state.collapsedWorkspaceIds.value).toEqual(['a'])
    expect(set).not.toHaveBeenCalled()
    state.setCollapsed('b', true)
    expect(set).toHaveBeenCalledExactlyOnceWith(key('user-a:org-a'), '["a","b"]')
  })

  it('disposes the scope watcher with its effect scope', () => {
    const identity = ref('user-a:org-a')
    const state = mount(identity)
    state.setCollapsed('a', true)
    scopes[scopes.length - 1]!.stop()
    const get = vi.spyOn(Storage.prototype, 'getItem')
    identity.value = 'user-a:org-b'
    expect(get).not.toHaveBeenCalled()
    expect(state.collapsedWorkspaceIds.value).toEqual(['a'])
  })
})
