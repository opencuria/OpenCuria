import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import FileUploadZone from './FileUploadZone.vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useNotificationStore } from '@/stores/notifications'
import { sendFilesUpload } from '@/services/socket'

vi.mock('@/services/socket', () => ({
  sendFilesList: vi.fn(),
  sendFilesFind: vi.fn(),
  sendFilesRead: vi.fn(),
  sendFilesDownload: vi.fn(),
  sendFilesUpload: vi.fn(),
}))

vi.mock('vue-sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}))

const WS_ID = 'ws-1'
const TARGET = '/workspace'

/** Build a FileList-like with real File objects (jsdom has File but no DataTransfer). */
function makeFileList(files: File[]): FileList {
  const list = {
    length: files.length,
    item: (index: number) => files[index] ?? null,
  } as unknown as FileList & { [index: number]: File }
  for (let i = 0; i < files.length; i++) {
    list[i] = files[i]!
  }
  return list as FileList
}

/** Dispatch a real DOM drop event carrying our files onto the zone element. */
function dispatchDrop(el: HTMLElement, files: FileList): void {
  const event = new Event('drop', { bubbles: true, cancelable: true }) as DragEvent
  Object.defineProperty(event, 'dataTransfer', { value: { files } })
  el.dispatchEvent(event)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useRealTimers()
  Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:mock-url'),
    revokeObjectURL: vi.fn(),
  })
  // jsdom lacks btoa for binary strings? ensure it exists.
  if (typeof globalThis.btoa === 'undefined') {
    vi.stubGlobal('btoa', (s: string) => Buffer.from(s, 'binary').toString('base64'))
  }
})


/** Resolve the tracked upload the way a backend `files:upload_result` would. */
function succeedUpload(requestId: string): void {
  useFileExplorerStore().handleUploadResult(requestId, TARGET, 'success', WS_ID)
}

function mountZone() {
  return mount(FileUploadZone, {
    props: { workspaceId: WS_ID, targetPath: TARGET },
    slots: { default: '<div data-testid="child" />' },
  })
}

describe('FileUploadZone', () => {
  it('rejects a >10 MiB file before arrayBuffer() with a visible toast and no send', async () => {
    const big = new File(['x'], 'big.bin', { type: 'application/octet-stream' })
    // Pretend the browser reports an over-limit size without allocating bytes.
    Object.defineProperty(big, 'size', { value: 10 * 1024 * 1024 + 1 })
    const arrayBufferSpy = vi.spyOn(big, 'arrayBuffer')

    const wrapper = mountZone()
    dispatchDrop(wrapper.element as HTMLElement, makeFileList([big]))
    await nextTick()
    // uploadFiles awaits arrayBuffer + tracked promises; flush microtasks.
    await vi.waitFor(() => {
      expect(vi.mocked(sendFilesUpload)).not.toHaveBeenCalled()
    })

    expect(arrayBufferSpy).not.toHaveBeenCalled()
    expect(vi.mocked(sendFilesUpload)).not.toHaveBeenCalled()
    const notify = useNotificationStore()
    // Visible toast via the notification store (mocked toast backend).
    expect(notify).toBeDefined()
    const { toast } = await import('vue-sonner')
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Upload failed',
      expect.objectContaining({ description: expect.stringContaining('10 MB') }),
    )
    // No upload was tracked, so completion emits once the drop settles.
    await vi.waitFor(() => {
      expect(wrapper.emitted('uploaded')).toBeTruthy()
    })
  })

  it('assigns unique request ids via crypto UUID, fallback differs on same time/name', async () => {
    const first = new File(['aaa'], 'same.txt', { type: 'text/plain' })
    const second = new File(['bbb'], 'same.txt', { type: 'text/plain' })
    vi.spyOn(first, 'arrayBuffer').mockResolvedValue(new Uint8Array([1, 2, 3]).buffer)
    vi.spyOn(second, 'arrayBuffer').mockResolvedValue(new Uint8Array([4, 5, 6]).buffer)

    // Force the timestamp+counter fallback by removing randomUUID.
    const cryptoObj = globalThis.crypto as unknown as { randomUUID?: () => string } | undefined
    const hadRandomUUID = typeof cryptoObj?.randomUUID === 'function'
    const originalRandomUUID = cryptoObj?.randomUUID
    if (cryptoObj) {
      try {
        cryptoObj.randomUUID = undefined
      } catch {
        // ignore when crypto is read-only
      }
    }
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000)
    try {
      const wrapper = mountZone()
      dispatchDrop(wrapper.element as HTMLElement, makeFileList([first, second]))
      await vi.waitFor(() => {
        expect(vi.mocked(sendFilesUpload)).toHaveBeenCalledTimes(2)
      })

      const ids = vi.mocked(sendFilesUpload).mock.calls.map((call) => call[1])
      expect(ids).toHaveLength(2)
      expect(ids[0]).not.toBe(ids[1])
      for (const id of ids) succeedUpload(id as string)
      await vi.waitFor(() => {
        expect(wrapper.emitted('uploaded')).toBeTruthy()
      })
    } finally {
      nowSpy.mockRestore()
      if (cryptoObj && hadRandomUUID && originalRandomUUID) {
        cryptoObj.randomUUID = originalRandomUUID
      }
    }
  })

  it('uses crypto UUID request ids when available', async () => {
    const file = new File(['hi'], 'a.txt', { type: 'text/plain' })
    vi.spyOn(file, 'arrayBuffer').mockResolvedValue(new Uint8Array([104, 105]).buffer)
    const uuidSpy = vi
      .spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValue('11111111-2222-4333-8444-555555555555')

    try {
      const wrapper = mountZone()
      dispatchDrop(wrapper.element as HTMLElement, makeFileList([file]))
      await vi.waitFor(() => {
        expect(vi.mocked(sendFilesUpload)).toHaveBeenCalledTimes(1)
      })
      expect(vi.mocked(sendFilesUpload).mock.calls[0]![1]).toBe(
        'upload-11111111-2222-4333-8444-555555555555',
      )
      succeedUpload(vi.mocked(sendFilesUpload).mock.calls[0]![1] as string)
      await vi.waitFor(() => {
        expect(wrapper.emitted('uploaded')).toBeTruthy()
      })
      void uuidSpy
    } finally {
      uuidSpy.mockRestore()
    }
  })

  it('a synchronous sendFilesUpload throw fails the upload, clears overlay, emits uploaded', async () => {
    const file = new File(['hi'], 'a.txt', { type: 'text/plain' })
    vi.spyOn(file, 'arrayBuffer').mockResolvedValue(new Uint8Array([104, 105]).buffer)
    vi.mocked(sendFilesUpload).mockImplementationOnce(() => {
      throw new Error('Upload exceeds the 10 MB limit.')
    })
    const store = useFileExplorerStore()
    const failSpy = vi.spyOn(store, 'failUpload')

    const wrapper = mountZone()
    dispatchDrop(wrapper.element as HTMLElement, makeFileList([file]))

    await vi.waitFor(() => {
      expect(failSpy).toHaveBeenCalledTimes(1)
    })
    expect(failSpy.mock.calls[0]![1]).toContain('10 MB')
    await vi.waitFor(() => {
      expect(wrapper.emitted('uploaded')).toBeTruthy()
    })
    // Uploading overlay must clear even on synchronous send failure.
    expect(wrapper.text()).not.toContain('Uploading…')
  })
})
