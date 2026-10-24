import { flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'

vi.mock('@/App.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/layout/AppLayout.vue', () => ({ default: { render: () => null } }))
vi.mock('@/views/ChatHomeView.vue', () => ({ default: { render: () => null } }))
vi.mock('@/services/socket', () => ({ connect: vi.fn() }))

afterEach(async () => {
  await flushPromises()
  vi.unstubAllGlobals()
  localStorage.clear()
  document.body.innerHTML = ''
})

it('restores an existing login through the configured backend after a reload', async () => {
  vi.resetModules()
  document.body.innerHTML = '<div id="app"></div>'
  localStorage.setItem(
    'kern_access_token',
    `header.${btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }))}.signature`,
  )
  let resolveConfig!: (value: Response) => void
  const configResponse = new Promise<Response>((resolve) => {
    resolveConfig = resolve
  })
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/config.json') return configResponse
    if (url === 'https://configured-backend.example/api/v1/auth/me/') {
      return new Response(
        JSON.stringify({
          id: 1,
          email: 'reload@example.test',
          first_name: '',
          last_name: '',
          organizations: [{ id: 'org-1', name: 'Reload', role: 'admin' }],
        }),
      )
    }
    throw new Error(`Unexpected request to ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  await import('./main')
  await flushPromises()
  try {
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(['/config.json'])
  } finally {
    resolveConfig(
      new Response(
        JSON.stringify({
          apiBaseUrl: 'https://configured-backend.example/api/v1',
        }),
      ),
    )
  }
  await vi.waitFor(() => {
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      '/config.json',
      'https://configured-backend.example/api/v1/auth/me/',
    ])
    expect(localStorage.getItem('kern_active_org_id')).toBe('org-1')
  })
})
