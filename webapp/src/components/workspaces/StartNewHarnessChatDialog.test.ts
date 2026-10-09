import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import StartNewHarnessChatDialog from './StartNewHarnessChatDialog.vue'

const { workspaceStore, harnessStore, skillStore, routerPush } = vi.hoisted(() => ({
  workspaceStore: {
    workspaces: [{ id: 'ws-1', name: 'Alpha' }],
    canUseWorkspace: vi.fn(() => true),
    fetchWorkspaces: vi.fn().mockResolvedValue(undefined),
  },
  harnessStore: {
    modelInput: '',
    effortInput: '',
    createSession: vi.fn(),
    setComposerModel: vi.fn(),
    setComposerEffort: vi.fn(),
  },
  skillStore: {
    skills: [],
    fetchSkills: vi.fn().mockResolvedValue(undefined),
  },
  routerPush: vi.fn(),
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: routerPush }) }))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => workspaceStore }))
vi.mock('@/stores/harness', () => ({ useHarnessStore: () => harnessStore }))
vi.mock('@/stores/skills', () => ({ useSkillStore: () => skillStore }))
vi.mock('@/components/ui/dialog', () => ({
  Dialog: {
    emits: ['update:open'],
    template:
      '<div><button data-testid="open-dialog" @click="$emit(\'update:open\', true)" /><slot /></div>',
  },
  DialogBody: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogTrigger: { template: '<div><slot /></div>' },
}))

function mountDialog() {
  return mount(StartNewHarnessChatDialog, {
    global: {
      stubs: {
        Button: { template: '<button><slot /></button>' },
        ScrollArea: { template: '<div><slot /></div>' },
        HarnessChatInput: {
          props: ['harnessId'],
          template: `
            <div>
              <button
                data-testid="select-claude"
                @click="$emit('update:harnessId', 'claude')"
              />
              <button
                data-testid="send-prompt"
                @click="$emit('send', 'hello', 'build', '', [], '', harnessId)"
              />
            </div>
          `,
        },
      },
    },
  })
}

describe('StartNewHarnessChatDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    workspaceStore.workspaces = [{ id: 'ws-1', name: 'Alpha' }]
    workspaceStore.canUseWorkspace.mockReturnValue(true)
    harnessStore.createSession.mockResolvedValue({ id: 'session-1' })
  })

  async function openPromptStep() {
    const wrapper = mountDialog()
    await wrapper.get('[data-testid="open-dialog"]').trigger('click')
    await wrapper.findAll('button')[1]!.trigger('click')
    await flushPromises()
    return wrapper
  }

  it('passes the selected Claude harness to session creation', async () => {
    const wrapper = await openPromptStep()

    await wrapper.get('[data-testid="select-claude"]').trigger('click')
    await wrapper.get('[data-testid="send-prompt"]').trigger('click')
    await flushPromises()

    expect(harnessStore.createSession).toHaveBeenCalledWith(
      'ws-1',
      'hello',
      'build',
      '',
      [],
      '',
      'claude',
    )
    expect(routerPush).toHaveBeenCalledWith({
      name: 'workspace-detail',
      params: { id: 'ws-1' },
      query: { session: 'session-1' },
    })
  })

  it('keeps the native harness as the default', async () => {
    const wrapper = await openPromptStep()

    await wrapper.get('[data-testid="send-prompt"]').trigger('click')
    await flushPromises()

    expect(harnessStore.createSession).toHaveBeenCalledWith(
      'ws-1',
      'hello',
      'build',
      '',
      [],
      '',
      'native',
    )
  })
})
