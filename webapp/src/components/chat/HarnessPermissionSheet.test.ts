import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessPermissionSheet from './HarnessPermissionSheet.vue'

function makeRequest() {
  return {
    request_id: 'req-1',
    session_id: 'session-1',
    workspace_id: 'workspace-1',
    tool: 'bash',
    pattern: 'rm -rf /tmp/x',
    title: '$ rm -rf /tmp/x',
    call_id: 'call-9',
  }
}

describe('HarnessPermissionSheet', () => {
  it('shows tool, title and pattern preview for the pending request', () => {
    const wrapper = mount(HarnessPermissionSheet, {
      props: { request: makeRequest() },
    })

    expect(wrapper.text()).toContain('bash')
    expect(wrapper.text()).toContain('$ rm -rf /tmp/x')
    expect(wrapper.text()).toContain('rm -rf /tmp/x')
  })

  it('emits the chosen resolution', async () => {
    const wrapper = mount(HarnessPermissionSheet, {
      props: { request: makeRequest() },
    })

    const buttons = wrapper.findAll('button')
    const approveOnce = buttons.find((button) => button.text().includes('Approve once'))
    expect(approveOnce).toBeTruthy()
    await approveOnce!.trigger('click')

    expect(wrapper.emitted('resolve')).toEqual([['once']])
  })
})
