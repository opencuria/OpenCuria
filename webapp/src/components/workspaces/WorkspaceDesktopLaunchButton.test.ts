import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import WorkspaceDesktopLaunchButton from './WorkspaceDesktopLaunchButton.vue'

describe('WorkspaceDesktopLaunchButton', () => {
  it('opens the only command directly when exactly one command exists', async () => {
    const wrapper = mount(WorkspaceDesktopLaunchButton, {
      props: {
        commands: [
          {
            id: 'cmd-1',
            workspace_id: 'workspace-1',
            name: 'Browser',
            command: '/usr/local/bin/opencuria-desktop-browser',
            created_at: '2026-04-01T10:00:00.000Z',
            updated_at: '2026-04-01T10:00:00.000Z',
          },
        ],
        canPrompt: true,
        desktopOpen: false,
        desktopMinimized: false,
      },
    })

    await wrapper.get('button').trigger('click')

    expect(wrapper.emitted('open')).toEqual([['cmd-1']])
    expect(wrapper.text()).not.toContain('Browser')
  })

  it('shows a dropdown only when multiple commands exist', async () => {
    const wrapper = mount(WorkspaceDesktopLaunchButton, {
      attachTo: document.body,
      props: {
        commands: [
          {
            id: 'cmd-1',
            workspace_id: 'workspace-1',
            name: 'Browser',
            command: '/usr/local/bin/opencuria-desktop-browser',
            created_at: '2026-04-01T10:00:00.000Z',
            updated_at: '2026-04-01T10:00:00.000Z',
          },
          {
            id: 'cmd-2',
            workspace_id: 'workspace-1',
            name: 'Docs',
            command: 'xdg-open https://docs.example.test',
            created_at: '2026-04-01T10:00:00.000Z',
            updated_at: '2026-04-01T10:00:00.000Z',
          },
        ],
        canPrompt: true,
        desktopOpen: false,
        desktopMinimized: false,
      },
    })

    await wrapper.get('button').trigger('click')

    expect(wrapper.emitted('open')).toBeUndefined()
    expect(document.body.textContent).toContain('Browser')
    expect(document.body.textContent).toContain('Docs')

    const optionButtons = [...document.body.querySelectorAll('button')].filter(
      (button) => button.textContent?.includes('Docs'),
    )
    expect(optionButtons).toHaveLength(1)
    optionButtons[0]!.click()

    expect(wrapper.emitted('open')).toEqual([['cmd-2']])
    wrapper.unmount()
  })
})
