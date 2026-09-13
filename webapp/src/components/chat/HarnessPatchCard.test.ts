import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import HarnessPatchCard from './HarnessPatchCard.vue'
import type { HarnessPart } from '@/types/harness'

const LARGE_DIFF = `--- a/CommandPalette.vue
+++ b/CommandPalette.vue
@@ -272,8 +272,9 @@
   <Dialog :open="props.open" @update:open="setOpen">
-  <DialogContent class="max-w-xl p-0" :show-close-button>
+  <DialogContent
+    class="gap-0 p-0 sm:max-w-2xl"
     extra
     lines
     here
     too
     more
`

function makePart(output: string, path = '/workspace/CommandPalette.vue'): HarnessPart {
  return {
    id: 'p1',
    session_id: 's1',
    type: 'patch',
    state: 'completed',
    title: `Patch ${path}`,
    output,
    meta: { path },
  }
}

describe('HarnessPatchCard', () => {
  it('shows the basename and +/- counts', () => {
    const wrapper = mount(HarnessPatchCard, {
      props: { part: makePart('--- a/a.txt\n+++ b/a.txt\n-old\n+new\n+also') },
    })
    expect(wrapper.get('[data-testid="harness-patch-name"]').text()).toBe(
      'CommandPalette.vue',
    )
    expect(wrapper.get('[data-testid="harness-patch-additions"]').text()).toBe('+2')
    expect(wrapper.get('[data-testid="harness-patch-deletions"]').text()).toBe('-1')
  })

  it('caps the collapsed preview at 4 lines', () => {
    const wrapper = mount(HarnessPatchCard, {
      props: { part: makePart(LARGE_DIFF) },
    })
    const rows = wrapper.findAll('[data-diff-type]')
    expect(rows).toHaveLength(4)
    expect(wrapper.text()).toContain('<Dialog :open')
    expect(wrapper.text()).not.toContain('more')
  })

  it('expands to the remaining diff lines', async () => {
    const wrapper = mount(HarnessPatchCard, {
      props: { part: makePart(LARGE_DIFF) },
    })
    await wrapper.get('[data-testid="harness-patch-header"]').trigger('click')
    expect(wrapper.findAll('[data-diff-type]').length).toBeGreaterThan(4)
    expect(wrapper.text()).toContain('extra')
  })
})
