import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import type { HarnessPart } from '@/types/harness'
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
