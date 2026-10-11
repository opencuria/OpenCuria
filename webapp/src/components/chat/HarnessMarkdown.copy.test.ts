import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import HarnessMarkdown from './HarnessMarkdown.vue'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'

vi.mock('@/services/socket', () => ({ sendFilesRead: vi.fn() }))
vi.mock('@/components/common/CopyButton.vue', () => ({
  default: {
    props: ['text', 'label', 'disabled'],
    template: '<button data-testid="code-copy" :disabled="disabled">{{ text }}</button>',
  },
}))

function mountMarkdown(text: string, streaming = false) {
  return mount(HarnessMarkdown, { props: { text, streaming } })
}

describe('HarnessMarkdown code block copying', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useWorkspaceImageStore().reset()
  })

  it('adds a copy action with exact decoded source text and keeps prose boundaries', () => {
    const wrapper = mountMarkdown('Before\n\n```ts\nconst value = "&"\n  next()\n```\n\nAfter')
    const block = wrapper.get('[data-testid="harness-code-block"]')
    const exactCode = block.get('code').element.textContent
    expect(exactCode).toBe('const value = "&"\n  next()\n')
    expect(block.get('[data-testid="code-copy"]').element.textContent).toBe(exactCode)
    expect(wrapper.find('p').text()).toBe('Before')
    const paragraphs = wrapper.findAll('p')
    expect(paragraphs[paragraphs.length - 1]?.text()).toBe('After')
    expect(wrapper.find('p code').exists()).toBe(false)
  })

  it('does not add a block copy action for inline code', () => {
    const wrapper = mountMarkdown('Use `const x = 1` here')
    expect(wrapper.get('code').text()).toBe('const x = 1')
    expect(wrapper.find('[data-testid="code-copy"]').exists()).toBe(false)
  })

  it('hides/disables assistant code copying during streaming but permits user markdown defaults', () => {
    const streaming = mountMarkdown('```js\nrun()\n```', true)
    expect(streaming.get('[data-testid="code-copy"]').attributes('disabled')).toBeDefined()

    const regular = mountMarkdown('```js\nrun()\n```')
    expect(regular.get('[data-testid="code-copy"]').attributes('disabled')).toBeUndefined()
  })

  it('adds copy buttons to nested list and blockquote fences without flattening their layout', () => {
    const wrapper = mountMarkdown(
      '- list item\n\n  ```c++\n  const x = 1\n  ```\n\n> quoted\n>\n> ```ts\n> const y = 2\n> ```',
    )

    expect(wrapper.find('ul > li').exists()).toBe(true)
    expect(wrapper.find('blockquote').exists()).toBe(true)
    const blocks = wrapper.findAll('[data-testid="harness-code-block"]')
    expect(blocks).toHaveLength(2)
    expect(blocks[0]!.get('[data-testid="code-copy"]').text()).toContain('const x = 1')
    expect(blocks[1]!.get('[data-testid="code-copy"]').text()).toContain('const y = 2')
    expect(blocks[0]!.get('code').classes()).toContain('language-c++')
    expect(blocks[1]!.get('code').classes()).toContain('language-ts')
  })

  it('preserves reference links across code fences and keeps workspace media beside them', () => {
    const wrapper = mountMarkdown(
      '[Guide][docs]\n\n![Photo][photo]\n\n```ts\nconst x = 1\n```\n\n[docs]: https://example.com/docs\n[photo]: /workspace/photo.png',
    )

    expect(wrapper.get('a').attributes('href')).toBe('https://example.com/docs')
    expect(wrapper.get('[data-testid="harness-markdown-media-fallback"]').text()).toBe('Photo')
    expect(wrapper.find('[data-testid="code-copy"]').exists()).toBe(true)
  })

  it('sanitizes raw HTML in fenced code and copies only text without language/fences', () => {
    const wrapper = mountMarkdown('```html\n<img src=x onerror=alert(1)>\n```')
    const code = wrapper.get('code')
    const exactCode = code.element.textContent
    expect(exactCode).toBe('<img src=x onerror=alert(1)>\n')
    expect(code.find('img').exists()).toBe(false)
    expect(wrapper.get('[data-testid="code-copy"]').element.textContent).toBe(exactCode)
    expect(wrapper.get('code').classes()).toContain('language-html')
  })

  it('keeps empty fences and punctuation-rich language identifiers safe', () => {
    const wrapper = mountMarkdown('```c++\n```')
    expect(wrapper.get('code').text()).toBe('')
    expect(wrapper.get('code').classes()).toContain('language-c++')
    expect(wrapper.find('[data-testid="code-copy"]').exists()).toBe(true)
  })
})
