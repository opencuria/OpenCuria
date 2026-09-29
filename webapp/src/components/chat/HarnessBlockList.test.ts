import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessBlockList from './HarnessBlockList.vue'
import type { RenderBlock } from '@/lib/harnessBlocks'
import type { HarnessPart } from '@/types/harness'

vi.mock('./HarnessMarkdown.vue', () => ({
  default: {
    props: ['text', 'compact'],
    template: '<div class="markdown-stub">{{ text }}</div>',
  },
}))

function makePart(index: number, type: HarnessPart['type']): HarnessPart {
  return {
    id: `part-${index}`,
    session_id: 'session-1',
    type,
    state: 'completed',
    tool: type === 'tool' ? 'read' : undefined,
    title: type === 'patch' ? `file-${index}.ts` : `Item ${index}`,
    output:
      type === 'text'
        ? `Note ${index}`
        : type === 'patch'
          ? '--- a/file.ts\n+++ b/file.ts\n-old\n+new'
          : 'ok',
    ...(type === 'patch' ? { meta: { path: `/workspace/file-${index}.ts` } } : {}),
  }
}

function makeMixedBlocks(count: number): RenderBlock[] {
  return Array.from({ length: count }, (_, index) => {
    const slot = index % 3
    const type = slot === 0 ? 'text' : slot === 1 ? 'tool' : 'patch'
    const part = makePart(index, type)
    return type === 'text'
      ? { kind: 'text' as const, part }
      : type === 'tool'
        ? { kind: 'single' as const, part }
        : { kind: 'card' as const, part }
  })
}

describe('HarnessBlockList', () => {
  it('pages large mixed timelines without losing block order or patch access', async () => {
    const blocks = makeMixedBlocks(1000)
    const wrapper = mount(HarnessBlockList, { props: { blocks } })

    expect(wrapper.findAll('[data-block-kind]')).toHaveLength(80)
    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(27)
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 1 of 13')
    expect(wrapper.find('[data-part-id="part-0"]').exists()).toBe(true)
    expect(wrapper.find('[data-part-id="part-80"]').exists()).toBe(false)

    for (let index = 0; index < 6; index += 1) {
      await wrapper.get('[data-testid="harness-page-next"]').trigger('click')
    }
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 7 of 13')
    expect(wrapper.findAll('[data-block-kind]')).toHaveLength(80)
    expect(wrapper.find('[data-part-id="part-480"]').exists()).toBe(true)
    expect(wrapper.find('[data-part-id="part-479"]').exists()).toBe(false)

    for (let index = 0; index < 6; index += 1) {
      await wrapper.get('[data-testid="harness-page-next"]').trigger('click')
    }
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 13 of 13')
    expect(wrapper.findAll('[data-block-kind]')).toHaveLength(40)
    expect(wrapper.find('[data-part-id="part-999"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="harness-patch-card"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('file-998.ts')
    expect(
      (wrapper.get('[data-testid="harness-page-next"]').element as HTMLButtonElement).disabled,
    ).toBe(true)
  }, 12_000)

  it('preserves the selected page across streamed output and appended timeline growth', async () => {
    const initial = makeMixedBlocks(1000)
    const wrapper = mount(HarnessBlockList, { props: { blocks: initial } })

    for (let index = 0; index < 4; index += 1) {
      await wrapper.get('[data-testid="harness-page-next"]').trigger('click')
    }
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 5 of 13')

    const streamed = initial.map((block) => {
      if (block.kind === 'group') return block
      return { ...block, part: { ...block.part, output: `${block.part.output} delta` } }
    })
    await wrapper.setProps({ blocks: streamed })
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 5 of 13')
    expect(wrapper.find('[data-part-id="part-320"]').exists()).toBe(true)

    await wrapper.setProps({
      blocks: [
        ...streamed,
        ...makeMixedBlocks(80).map((block, index) => {
          if (block.kind === 'group') return block
          return { ...block, part: { ...block.part, id: `new-${index}` } }
        }),
      ],
    })
    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 5 of 14')
    expect(wrapper.find('[data-part-id="part-320"]').exists()).toBe(true)
  })

  it('keeps the live text and cursor mounted when streaming exceeds one page', async () => {
    const blocks = makeMixedBlocks(80)
    blocks[79] = { kind: 'text', part: makePart(79, 'text') }
    const wrapper = mount(HarnessBlockList, {
      props: { blocks, showStreamingCursor: true },
    })

    expect(wrapper.find('[data-testid="harness-page-controls"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-block-kind]')).toHaveLength(80)
    expect(wrapper.find('[data-part-id="part-79"] .animate-pulse').exists()).toBe(true)

    const grown = [
      ...blocks,
      ...makeMixedBlocks(4).map((block, index) => {
        if (block.kind === 'group') return block
        return { ...block, part: { ...block.part, id: `live-${index}` } }
      }),
    ]
    grown[grown.length - 1] = {
      kind: 'text',
      part: { ...makePart(100, 'text'), output: 'Live answer' },
    }
    await wrapper.setProps({ blocks: grown })

    expect(wrapper.find('[data-testid="harness-page-status"]')!.text()).toContain('Page 2 of 2')
    expect(wrapper.find('[data-block-kind]').exists()).toBe(true)
    expect(wrapper.find('[data-part-id="part-79"]').exists()).toBe(false)
    expect(wrapper.find('[data-part-id="part-100"]').text()).toContain('Live answer')
    expect(wrapper.find('[data-part-id="part-100"] .animate-pulse').exists()).toBe(true)
  })

  it('keeps all normal-sized timelines unchanged without pagination controls', () => {
    const blocks = makeMixedBlocks(6)
    const wrapper = mount(HarnessBlockList, { props: { blocks } })

    expect(wrapper.findAll('[data-block-kind]')).toHaveLength(6)
    expect(wrapper.findAll('[data-testid="harness-work-row"]')).toHaveLength(2)
    expect(wrapper.findAll('[data-testid="harness-patch-card"]')).toHaveLength(2)
    expect(wrapper.find('[data-testid="harness-page-controls"]').exists()).toBe(false)
  })
})
