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
      props: { requests: [makeRequest()] },
    })

    expect(wrapper.text()).toContain('bash')
    expect(wrapper.text()).toContain('$ rm -rf /tmp/x')
    expect(wrapper.text()).toContain('rm -rf /tmp/x')
  })

  it('emits the chosen resolution', async () => {
    const wrapper = mount(HarnessPermissionSheet, {
      props: { requests: [makeRequest()] },
    })

    const buttons = wrapper.findAll('button')
    const approveOnce = buttons.find((button) => button.text().includes('Approve once'))
    expect(approveOnce).toBeTruthy()
    await approveOnce!.trigger('click')

    expect(wrapper.emitted('resolve')).toEqual([['req-1', 'once']])
  })

  it('pages through multiple pending permission requests', async () => {
    const wrapper = mount(HarnessPermissionSheet, {
      props: {
        requests: [
          makeRequest(),
          {
            ...makeRequest(),
            request_id: 'req-2',
            call_id: 'call-10',
            title: '$ git diff',
            pattern: 'git diff',
          },
        ],
      },
    })

    expect(wrapper.get('[data-testid="composer-permission-page"]').text()).toBe('1 of 2')
    expect(wrapper.text()).toContain('$ rm -rf /tmp/x')
    await wrapper.get('[data-testid="composer-permission-next"]').trigger('click')
    expect(wrapper.get('[data-testid="composer-permission-page"]').text()).toBe('2 of 2')
    expect(wrapper.text()).toContain('$ git diff')
    const buttons = wrapper.findAll('button')
    const always = buttons.find((button) => button.text().includes('Always allow'))
    await always!.trigger('click')
    expect(wrapper.emitted('resolve')).toEqual([['req-2', 'always']])
  })
})
