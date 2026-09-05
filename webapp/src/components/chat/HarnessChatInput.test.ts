import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessChatInput from './HarnessChatInput.vue'
import * as harnessApi from '@/services/harness.api'
import type { ProviderModel } from '@/lib/harnessModels'
import { useFileExplorerStore } from '@/stores/fileExplorer'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    getProviderConfig: vi.fn(),
    listProviderModels: vi.fn(),
  }
})

const getProviderConfigMock = vi.mocked(harnessApi.getProviderConfig)
const listProviderModelsMock = vi.mocked(harnessApi.listProviderModels)

const catalog: ProviderModel[] = [
  {
    id: 'model-big',
    name: 'Big',
    reasoning_efforts: ['low', 'high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 200_000,
    max_output_tokens: 32_768,
  },
  {
    id: 'model-small',
    name: 'Small',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
]

const dropdownStubs = {
  RouterLink: {
    template: '<a :href="to"><slot /></a>',
    props: ['to'],
  },
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: { template: '<button type="button"><slot /></button>' },
  DropdownMenuSub: { template: '<div><slot /></div>' },
  DropdownMenuSubTrigger: { template: '<div><slot /></div>' },
  DropdownMenuSubContent: { template: '<div><slot /></div>' },
  DropdownMenuSeparator: { template: '<hr />' },
}

function mountInput(props: Record<string, unknown> = {}) {
  return mount(HarnessChatInput, {
    props: { workspaceId: 'ws-1', ...props },
    global: { stubs: dropdownStubs },
  })
}

describe('HarnessChatInput', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-big',
      small_model: 'model-small',
      computer_use_model: 'model-cu',
      has_api_key: true,
      api_key_hint: '',
    })
    listProviderModelsMock.mockResolvedValue(catalog)
  })

  it('loads the OpenRouter catalog into the model picker', async () => {
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(listProviderModelsMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    const html = wrapper.html()
    expect(html).toContain('Auto')
    expect(html).toContain('Big')
    expect(html).toContain('Small')
    expect(wrapper.text()).not.toContain('Skills')
    expect(wrapper.text()).not.toContain('Fast')
  })

  it('renders mode pill, context ring, paperclip, and send arrow', async () => {
    const wrapper = mountInput()
    expect(wrapper.find('[data-testid="composer-mode-trigger"]').exists()).toBe(true)
    const ring = wrapper.find('[data-testid="composer-context-usage"]')
    const attach = wrapper.find('[data-testid="composer-attach"]')
    expect(ring.exists()).toBe(true)
    expect(attach.exists()).toBe(true)
    expect(ring.element.compareDocumentPosition(attach.element)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
    expect(wrapper.find('[data-testid="composer-send"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="composer-textarea"]').attributes('placeholder')).toContain(
      '/ for skills',
    )
  })

  it('shows org settings CTA when provider config is missing', async () => {
    getProviderConfigMock.mockRejectedValue(new Error('not found'))
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    const link = wrapper.find('a[href="/org-settings?tab=provider"]')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('Configure OpenRouter in Org Settings')
  })

  it('shows org settings CTA when provider config has no API key', async () => {
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-big',
      small_model: 'model-small',
      computer_use_model: 'model-cu',
      has_api_key: false,
      api_key_hint: '',
    })
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('a[href="/org-settings?tab=provider"]').exists()).toBe(true)
    expect(listProviderModelsMock).not.toHaveBeenCalled()
  })

  it('toggles plan/build with Shift+Tab', async () => {
    const wrapper = mountInput({ mode: 'build' })
    await wrapper.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(wrapper.emitted('update:mode')?.[0]).toEqual(['plan'])
    await wrapper.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(wrapper.emitted('update:mode')?.[1]).toEqual(['build'])
  })

  it('sends prompt, mode, model, skill ids, and effort', async () => {
    const wrapper = mountInput({ mode: 'plan' })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('hello world')
    await textarea.trigger('keydown', { key: 'Enter' })
    const sends = wrapper.emitted('send') ?? []
    expect(sends.length).toBeGreaterThan(0)
    expect(sends[0]![0]).toBe('hello world')
    expect(sends[0]![1]).toBe('plan')
    expect(typeof sends[0]![2]).toBe('string')
    expect(sends[0]![3]).toEqual([])
    expect(typeof sends[0]![4]).toBe('string')
  })

  it('attaches a skill from the slash picker and includes it on send', async () => {
    const wrapper = mountInput({
      skillOptions: [
        {
          id: 'skill-1',
          name: 'Lint rules',
          body: 'Always lint',
          scope: 'personal',
          created_by_email: null,
          created_at: '2026-03-29T10:00:00.000Z',
          updated_at: '2026-03-29T10:00:00.000Z',
        },
      ],
    })
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })

    const textarea = wrapper.find('textarea')
    await textarea.setValue('/Lin')
    textarea.element.setSelectionRange(4, 4)
    await textarea.trigger('input')
    expect(wrapper.find('[role="listbox"]').text()).toContain('Lint rules')
    await textarea.trigger('keydown', { key: 'Enter' })
    expect(wrapper.text()).toContain('Lint rules')

    await textarea.setValue('use skills')
    await textarea.trigger('keydown', { key: 'Enter' })
    const sends = wrapper.emitted('send') ?? []
    expect(sends.length).toBeGreaterThan(0)
    expect(sends[0]![3]).toEqual(['skill-1'])
  })

  it('mirrors mention state to the parent sheet stack in controlled mode', async () => {
    const wrapper = mountInput({
      mentionControlled: true,
      files: [{ name: 'a.ts', path: '/workspace/a.ts', type: 'file', size: 1 }],
    })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('@a')
    textarea.element.setSelectionRange(2, 2)
    await textarea.trigger('input')

    const changes = wrapper.emitted('mention-change') ?? []
    expect(changes.length).toBeGreaterThan(0)
    const last = changes[changes.length - 1]!
    expect(last[0]).toBe(true)
    expect(Array.isArray(last[2])).toBe(true)
    expect((last[2] as Array<{ label: string }>).length).toBeGreaterThan(0)
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
  })

  it('emits mention-select instead of inserting in controlled mode', async () => {
    const wrapper = mountInput({
      mentionControlled: true,
      files: [{ name: 'a.ts', path: '/workspace/a.ts', type: 'file', size: 1 }],
    })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('@a')
    textarea.element.setSelectionRange(2, 2)
    await textarea.trigger('input')
    await textarea.trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('mention-select')?.length).toBeGreaterThan(0)
    expect((wrapper.emitted('send') ?? []).length).toBe(0)
  })

  it('fetches workspace files when @ is typed', async () => {
    const store = useFileExplorerStore()
    const findFiles = vi.spyOn(store, 'findFiles').mockResolvedValue(['/workspace/src/a.ts'])
    const wrapper = mountInput()
    const textarea = wrapper.find('textarea')
    await textarea.setValue('@src')
    textarea.element.setSelectionRange(4, 4)
    await textarea.trigger('input')
    expect(findFiles).toHaveBeenCalledWith('ws-1', 'src', 50)
  })

  it('moves mention selection with arrow keys', async () => {
    const store = useFileExplorerStore()
    vi.spyOn(store, 'findFiles').mockResolvedValue([])
    const wrapper = mountInput({
      files: [
        { name: 'a.ts', path: '/workspace/a.ts', type: 'file', size: 1 },
        { name: 'b.ts', path: '/workspace/b.ts', type: 'file', size: 1 },
      ],
    })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('@')
    textarea.element.setSelectionRange(1, 1)
    await textarea.trigger('input')
    const options = wrapper.findAll('[role="option"]')
    expect(options.length).toBeGreaterThan(1)
    expect(options[0]!.attributes('aria-selected')).toBe('true')
    await textarea.trigger('keydown', { key: 'ArrowDown' })
    const moved = wrapper.findAll('[role="option"]')
    expect(moved[1]!.attributes('aria-selected')).toBe('true')
  })

  it('emits toggle-context when the usage ring is clicked', async () => {
    const wrapper = mountInput({ contextUsed: 12_000 })
    await wrapper.find('[data-testid="composer-context-usage"]').trigger('click')
    expect(wrapper.emitted('toggle-context')).toEqual([[]])
  })

  it('fills the context ring from catalog limit and used tokens', async () => {
    const wrapper = mountInput({ contextUsed: 50_000, model: 'model-big' })
    await vi.waitFor(() => {
      expect(listProviderModelsMock).toHaveBeenCalled()
    })
    const rings = wrapper.findAll('[data-testid="composer-context-usage"] circle')
    expect(rings).toHaveLength(2)
    const circumference = 2 * Math.PI * 6
    const progress = rings[1]!
    expect(progress.attributes('stroke-dashoffset')).toBe(String(circumference * 0.75))
    expect(wrapper.find('[data-testid="composer-context-usage"]').attributes('aria-label')).toBe(
      'Context usage 25%',
    )
  })

  it('emits context metrics when used tokens or catalog limit change', async () => {
    const wrapper = mountInput({ contextUsed: 50_000, model: 'model-big' })
    await vi.waitFor(() => {
      expect(listProviderModelsMock).toHaveBeenCalled()
    })
    const metrics = wrapper.emitted('context-metrics') ?? []
    expect(metrics.length).toBeGreaterThan(0)
    const last = metrics[metrics.length - 1]![0] as {
      used: number
      limit: number
      percent: number
    }
    expect(last.used).toBe(50_000)
    expect(last.limit).toBe(200_000)
    expect(last.percent).toBe(25)
  })

  it('keeps rounded corners and an accent focus ring when a sheet is attached', () => {
    const wrapper = mountInput({ attached: true })
    const card = wrapper.find('[data-testid="composer-card"]')
    expect(card.exists()).toBe(true)
    expect(card.classes()).toContain('rounded-xl')
    expect(card.classes()).toContain('focus-within:border-primary')
    expect(card.classes()).not.toContain('rounded-t-none')
    expect(card.classes()).not.toContain('border-t-0')
  })
})
