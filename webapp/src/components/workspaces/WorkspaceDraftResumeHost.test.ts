import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { reactive, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import WorkspaceDraftResumeHost from './WorkspaceDraftResumeHost.vue'
import { saveWorkspaceDraft } from '@/lib/workspaceDraft'

const routeState = reactive({ path: '/', query: {} as Record<string, unknown> })
const routerReplace = vi.fn(async ({ query }: { query: Record<string, unknown> }) => {
  routeState.query = query
})
vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => ({ replace: routerReplace }),
}))
const auth = vi.hoisted(() => ({
  initialized: true,
  user: { id: 'user-1' },
  activeOrganizationId: 'org-1',
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
const workspaceStore = vi.hoisted(() => ({
  workspaces: [],
  fetchWorkspaces: vi.fn(async () => undefined),
}))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => workspaceStore }))
vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: vi.fn() }),
}))
vi.mock('./CreateWorkspaceDialog.vue', () => ({
  default: {
    props: ['resumeDraftId'],
    emits: ['handoff', 'created', 'close'],
    template: '<div data-testid="resumed-create">{{ resumeDraftId }}</div>',
  },
}))
vi.mock('./EditWorkspaceDialog.vue', () => ({ default: { template: '<div />' } }))

function createDraft(name: string): string {
  const draft = saveWorkspaceDraft(
    { mode: 'create', name, credentialIds: [], pluginIds: [], repos: [] },
    { userId: 'user-1', organizationId: 'org-1' },
    '/workspaces',
  )
  expect(draft.error).toBeUndefined()
  return draft.id
}

describe('WorkspaceDraftResumeHost', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    routeState.path = '/'
    routeState.query = {}
    routerReplace.mockClear()
    workspaceStore.workspaces = []
    sessionStorage.clear()
  })

  it('does not let a prior close animation hide a later create-draft resume', async () => {
    const firstId = createDraft('denied authorization draft')
    const secondId = createDraft('retry authorization draft')
    const wrapper = mount(WorkspaceDraftResumeHost)

    routeState.query = { resume_workspace: firstId }
    await flushPromises()
    expect(wrapper.get('[data-testid="resumed-create"]').text()).toBe(firstId)

    // The actual dialog emits `handoff` immediately before it closes itself
    // and routes to credentials; that event must not clear the saved draft.
    await wrapper.get('[data-testid="resumed-create"]').trigger('click')
    wrapper.findComponent({ name: 'CreateWorkspaceDialog' }).vm.$emit('handoff')
    await nextTick()
    routeState.query = { settings: 'credentials' }
    await flushPromises()
    expect(wrapper.find('[data-testid="resumed-create"]').exists()).toBe(false)

    routeState.query = { resume_workspace: secondId }
    await flushPromises()
    expect(wrapper.get('[data-testid="resumed-create"]').text()).toBe(secondId)
    wrapper.findComponent({ name: 'CreateWorkspaceDialog' }).vm.$emit('close')
    await nextTick()
    expect(wrapper.get('[data-testid="resumed-create"]').text()).toBe(secondId)
    wrapper.unmount()
  })
})
