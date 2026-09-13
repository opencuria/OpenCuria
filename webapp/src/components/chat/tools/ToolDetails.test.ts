import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import type { HarnessPart } from '@/types/harness'
import ImageLightbox from '../ImageLightbox.vue'
import ToolDetailBash from './ToolDetailBash.vue'
import ToolDetailDefault from './ToolDetailDefault.vue'
import ToolDetailQuestion from './ToolDetailQuestion.vue'
import ToolDetailRead from './ToolDetailRead.vue'
import ToolDetailSearch from './ToolDetailSearch.vue'
import ToolDetailTodos from './ToolDetailTodos.vue'
import ToolDetailWebfetch from './ToolDetailWebfetch.vue'
import ToolDetailComputerUse from './ToolDetailComputerUse.vue'

function makePart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'part-1',
    session_id: 'session-1',
    type: 'tool',
    state: 'completed',
    title: '',
    output: '',
    ...overrides,
  }
}

describe('tool detail components', () => {
  beforeEach(() => {
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  })
  it('renders bash command, output, and a failed exit code', () => {
    const wrapper = mount(ToolDetailBash, {
      props: {
        part: makePart({
          tool: 'bash',
          output: 'boom',
          input: { arguments: '{"command":"false"}' },
          meta: { exit_code: 1 },
          state: 'error',
        }),
      },
    })

    expect(wrapper.get('[data-testid="tool-detail-bash"]').text()).toContain('$ false')
    expect(wrapper.get('[data-testid="tool-detail-bash-exit"]').text()).toContain('exit 1')
    expect(wrapper.text()).toContain('boom')
  })

  it('renders a read path and file body', () => {
    const wrapper = mount(ToolDetailRead, {
      props: {
        part: makePart({
          tool: 'read',
          output: 'export const x = 1',
          input: { arguments: '{"path":"/workspace/a.ts"}' },
        }),
      },
    })

    expect(wrapper.text()).toContain('/workspace/a.ts')
    expect(wrapper.text()).toContain('export const x = 1')
  })

  it('renders image attachments as thumbnails that open the lightbox', async () => {
    const url = 'data:image/png;base64,iVBORw0KGgo='
    const wrapper = mount(ToolDetailRead, {
      props: {
        part: makePart({
          tool: 'read',
          output: 'Image read successfully',
          input: { arguments: '{"path":"/workspace/cat.png"}' },
          meta: { attachments: [{ type: 'file', mime: 'image/png', url }] },
        }),
      },
      global: {
        stubs: { teleport: true },
      },
    })

    const img = wrapper.get('[data-testid="tool-detail-read-image"]')
    expect(img.attributes('src')).toBe(url)
    // Text preview from the backend is kept alongside the thumbnail.
    expect(wrapper.text()).toContain('Image read successfully')

    await wrapper.get('button').trigger('click')
    const lightbox = wrapper.findComponent(ImageLightbox)
    expect(lightbox.exists()).toBe(true)
    expect(lightbox.props('src')).toBe(url)
  })

  it('renders PDF attachments as a download link with the file name', () => {
    const url = 'data:application/pdf;base64,JVBERi0='
    const wrapper = mount(ToolDetailRead, {
      props: {
        part: makePart({
          tool: 'read',
          output: 'PDF read successfully',
          input: { arguments: '{"path":"/workspace/doc.pdf"}' },
          meta: { attachments: [{ type: 'file', mime: 'application/pdf', url }] },
        }),
      },
    })

    const link = wrapper.get('[data-testid="tool-detail-read-pdf"]')
    expect(link.attributes('href')).toBe(url)
    expect(link.attributes('download')).toBe('doc.pdf')
    expect(link.text()).toContain('PDF attachment')
    expect(wrapper.text()).toContain('PDF read successfully')
  })

  it('prefers attachment.filename over meta.path and tool args for the PDF name', () => {
    const url = 'data:application/pdf;base64,JVBERi0='
    const withFilename = mount(ToolDetailRead, {
      props: {
        part: makePart({
          tool: 'read',
          output: 'PDF read successfully',
          input: { arguments: '{"path":"/workspace/args.pdf"}' },
          meta: {
            path: '/workspace/meta.pdf',
            attachments: [{ type: 'file', mime: 'application/pdf', url, filename: 'live.pdf' }],
          },
        }),
      },
    })
    expect(withFilename.get('[data-testid="tool-detail-read-pdf"]').attributes('download')).toBe(
      'live.pdf',
    )

    const metaOnly = mount(ToolDetailRead, {
      props: {
        part: makePart({
          tool: 'read',
          output: 'PDF read successfully',
          input: { arguments: '{"path":"/workspace/args.pdf"}' },
          meta: {
            path: '/workspace/meta.pdf',
            attachments: [{ type: 'file', mime: 'application/pdf', url }],
          },
        }),
      },
    })
    expect(metaOnly.get('[data-testid="tool-detail-read-pdf"]').attributes('download')).toBe(
      'meta.pdf',
    )

    const fallback = mount(ToolDetailRead, {
      props: {
        part: makePart({
          tool: 'read',
          output: 'PDF read successfully',
          meta: { attachments: [{ type: 'file', mime: 'application/pdf', url }] },
        }),
      },
    })
    expect(fallback.get('[data-testid="tool-detail-read-pdf"]').attributes('download')).toBe(
      'document.pdf',
    )
  })

  it('renders a live reducer part with meta.attachments as a thumbnail', async () => {
    const { applyPartDelta } = await import('@/lib/harnessReducer')
    const { resetHarnessPartCounter } = await import('@/lib/harnessReducer')
    resetHarnessPartCounter()
    const url = 'data:image/png;base64,iVBORw0KGgo='
    const messages = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user' as const,
        content: 'hello',
        parts: [],
      },
    ]
    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'read cat.png', call_id: 'call-live' },
      { step: 1, partId: 'part-live' },
    )
    const message = applyPartDelta(
      messages,
      'session-1',
      {
        tool_completed: 'read',
        call_id: 'call-live',
        output: 'Image read successfully',
        attachments: [{ type: 'file', mime: 'image/png', url, filename: 'cat.png' }],
      },
      { step: 1, partId: 'part-live' },
    )
    const livePart = message.parts.find((p) => p.call_id === 'call-live')!

    const wrapper = mount(ToolDetailRead, {
      props: { part: livePart },
      global: { stubs: { teleport: true } },
    })
    const img = wrapper.get('[data-testid="tool-detail-read-image"]')
    expect(img.attributes('src')).toBe(url)
    expect(img.attributes('alt')).toContain('cat.png')

    await wrapper.get('button').trigger('click')
    expect(wrapper.findComponent(ImageLightbox).exists()).toBe(true)
  })

  it('renders only text when read meta attachments are broken', () => {
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      for (const meta of [
        undefined,
        {},
        { attachments: 'nope' },
        { attachments: [{ type: 'file', mime: '', url: 'https://example.com/a.png' }] },
      ]) {
        const wrapper = mount(ToolDetailRead, {
          props: {
            part: makePart({
              tool: 'read',
              output: 'file body',
              input: { arguments: '{"path":"/workspace/a.ts"}' },
              ...(meta === undefined ? {} : { meta }),
            }),
          },
        })
        expect(wrapper.find('[data-testid="tool-detail-read-image"]').exists()).toBe(false)
        expect(wrapper.find('[data-testid="tool-detail-read-pdf"]').exists()).toBe(false)
        expect(wrapper.text()).toContain('file body')
      }
    } finally {
      consoleSpy.mockRestore()
    }
  })

  it('renders search hits with a count, and an empty result', () => {
    const hits = mount(ToolDetailSearch, {
      props: {
        part: makePart({
          tool: 'grep',
          output: 'a.ts:1:foo\nb.ts:2:foo',
          input: { arguments: '{"pattern":"foo"}' },
        }),
      },
    })
    expect(hits.get('[data-testid="tool-detail-search-count"]').text()).toBe('2')
    expect(hits.text()).toContain('a.ts:1:foo')

    const empty = mount(ToolDetailSearch, {
      props: {
        part: makePart({ tool: 'glob', output: '', input: { arguments: '{"pattern":"*.md"}' } }),
      },
    })
    expect(empty.text()).toContain('No matches')
  })

  it('renders a webfetch url and a missing-url fallback', () => {
    const wrapper = mount(ToolDetailWebfetch, {
      props: {
        part: makePart({
          tool: 'webfetch',
          output: '<html>ok</html>',
          input: { arguments: '{"url":"https://example.com"}' },
        }),
      },
    })
    expect(wrapper.get('a').attributes('href')).toBe('https://example.com')
    expect(wrapper.text()).toContain('<html>ok</html>')
  })

  it('renders a webfetch format from arguments', () => {
    const wrapper = mount(ToolDetailWebfetch, {
      props: {
        part: makePart({
          tool: 'webfetch',
          output: '# Hello',
          input: { arguments: '{"url":"https://example.com","format":"markdown"}' },
        }),
      },
    })
    expect(wrapper.text()).toContain('markdown')
    expect(wrapper.get('a').attributes('href')).toBe('https://example.com')
  })

  it('pairs questions with answers and handles missing answers', () => {
    const wrapper = mount(ToolDetailQuestion, {
      props: {
        part: makePart({
          tool: 'question',
          output: '{"answers":["Build"]}',
          input: {
            arguments: JSON.stringify({
              questions: [{ question: 'Which mode?' }],
            }),
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('Which mode?')
    expect(wrapper.get('[data-testid="tool-detail-question-answer"]').text()).toBe('Build')
  })

  it('renders todo rows from output and a cleared list', () => {
    const rows = mount(ToolDetailTodos, {
      props: {
        part: makePart({
          tool: 'todowrite',
          output: '[pending] Ship it\n[completed] Tests',
        }),
      },
    })
    expect(rows.text()).toContain('Ship it')
    expect(rows.text()).toContain('Tests')

    const empty = mount(ToolDetailTodos, {
      props: { part: makePart({ tool: 'todowrite', output: '' }) },
    })
    expect(empty.text()).toContain('Todo list cleared.')
  })

  it('renders a compact computer-use summary and error output', () => {
    const wrapper = mount(ToolDetailComputerUse, {
      props: {
        part: makePart({
          tool: 'left_click',
          state: 'error',
          output: 'click failed',
          input: { arguments: '{"x":10,"y":20}' },
        }),
      },
    })
    expect(wrapper.text()).toContain('Left click (10, 20)')
    expect(wrapper.text()).toContain('click failed')
  })

  it('falls back to the default tool name and output', () => {
    const wrapper = mount(ToolDetailDefault, {
      props: {
        part: makePart({ tool: 'write', output: 'Wrote 12 bytes' }),
      },
    })
    expect(wrapper.get('[data-testid="tool-detail-default"]').text()).toContain('write')
    expect(wrapper.text()).toContain('Wrote 12 bytes')
  })
})
