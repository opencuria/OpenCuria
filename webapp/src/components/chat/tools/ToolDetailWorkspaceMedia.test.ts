import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'

import type { HarnessPart } from '@opencuria/chat-core/harness'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import {
  makeFileLink,
  resetWorkspaceFiles,
  workspaceFiles,
} from '@/services/workspaceFiles.fixtures'
import ToolDetailDefault from './ToolDetailDefault.vue'
import ToolDetailRead from './ToolDetailRead.vue'

const openPath = vi.fn()

vi.mock('@/services/workspaceFiles', () => import('@/services/workspaceFiles.fixtures'))
vi.mock('@/stores/fileExplorer', () => ({
  useFileExplorerStore: () => ({ openPath }),
}))

const IMAGE = '/workspace/.opencuria/tool-media/abc.png'
const PDF = '/workspace/.opencuria/tool-media/def.pdf'

function readPart(attachments: Record<string, unknown>[]): HarnessPart {
  return {
    id: 'part-1',
    session_id: 'session-1',
    type: 'tool',
    state: 'completed',
    title: 'Read cat.png',
    tool: 'read',
    output: 'Image read successfully',
    input: { arguments: '{"path":"/workspace/designs/cat.png"}' },
    meta: { attachments },
  }
}

function mountDetail(component: typeof ToolDetailRead, part: HarnessPart, workspaceId = 'ws-1') {
  return mount(component, {
    props: { part },
    global: {
      provide: { [harnessWorkspaceIdKey as symbol]: ref(workspaceId) },
      stubs: { teleport: true },
    },
  })
}

describe('workspace tool media', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetWorkspaceFiles()
    openPath.mockClear()
  })

  it('loads Read images from the workspace and labels them with the source file', async () => {
    const wrapper = mountDetail(
      ToolDetailRead,
      readPart([{ type: 'file', mime: 'image/png', path: IMAGE }]),
    )
    expect(workspaceFiles().link).toHaveBeenCalledWith('ws-1', IMAGE, expect.any(AbortSignal))
    expect(wrapper.find('[data-testid="tool-detail-read-image"]').exists()).toBe(false)

    await flushPromises()
    const image = wrapper.get('[data-testid="workspace-media-image"]')
    expect(image.attributes('src')).toBe(`https://files.test/content${IMAGE}`)
    expect(image.attributes('alt')).toBe('cat.png')
    expect(image.attributes('data-voice-image-path')).toBe(IMAGE)
    expect(image.attributes('data-voice-workspace')).toBe('ws-1')
  })

  it('loads generated images above the autoload limit only on tap', async () => {
    vi.mocked(workspaceFiles().link).mockResolvedValue(
      makeFileLink(IMAGE, { size: 12 * 1024 * 1024 }),
    )
    const wrapper = mountDetail(
      ToolDetailDefault,
      readPart([{ type: 'file', mime: 'image/png', path: IMAGE }]),
    )
    await flushPromises()
    expect(wrapper.get('[data-testid="workspace-media-placeholder"]').text()).toContain('12.0 MB')

    await wrapper.get('[data-testid="workspace-media-load"]').trigger('click')
    expect(wrapper.get('[data-testid="workspace-media-image"]').attributes('src')).toBe(
      `https://files.test/content${IMAGE}`,
    )
  })

  it('opens workspace PDFs in the editor instead of linking a data URL', async () => {
    const wrapper = mountDetail(
      ToolDetailRead,
      readPart([{ type: 'file', mime: 'application/pdf', path: PDF, filename: 'spec.pdf' }]),
    )
    const pdf = wrapper.get('[data-testid="tool-detail-read-pdf"]')
    expect(pdf.element.tagName).toBe('BUTTON')
    expect(pdf.text()).toContain('spec.pdf')
    await pdf.trigger('click')
    expect(openPath).toHaveBeenCalledWith('ws-1', PDF, { revealInExplorer: false })
  })

  it('shows a file tile without reading when no workspace is available', () => {
    const wrapper = mountDetail(
      ToolDetailDefault,
      readPart([{ type: 'file', mime: 'image/png', path: IMAGE }]),
      '',
    )
    expect(workspaceFiles().link).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="mention-image-fallback"]').text()).toBe('Image attachment 1')
  })
})
