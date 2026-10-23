/**
 * Pinia store for managing workspace images.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'
import type {
  CapturedImage,
  ImageArtifactCloneIn,
  ImageDefinition,
  RunnerImageBuild,
} from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { useNotificationStore } from './notifications'

export const useImageStore = defineStore('images', () => {
  const notifications = useNotificationStore()

  const capturedImages = ref<CapturedImage[]>([])
  const imageDefinitions = ref<ImageDefinition[]>([])
  const runnerBuildsByDefinition = ref<Record<string, RunnerImageBuild[]>>({})
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchCapturedImages(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      capturedImages.value = await workspacesApi.listCapturedImages()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to load images'
    } finally {
      loading.value = false
    }
  }

  async function renameCapturedImage(id: string, name: string): Promise<boolean> {
    try {
      const updated = await workspacesApi.renameCapturedImage(id, name)
      const idx = capturedImages.value.findIndex((image) => image.id === id)
      if (idx !== -1) capturedImages.value[idx] = updated
      notifications.success('Image renamed', `Image renamed to "${name}".`)
      return true
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to rename image'
      notifications.error('Rename failed', msg)
      return false
    }
  }

  /** New workspaces always start from the latest version of an image. */
  async function createWorkspaceFromCapturedImage(
    capturedImageId: string,
    data: ImageArtifactCloneIn,
  ): Promise<string | null> {
    try {
      const result = await workspacesApi.createWorkspaceFromCapturedImage(capturedImageId, data)
      notifications.success('Creating workspace', 'New workspace is being created from image.')
      return result.workspace_id
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to create workspace'
      notifications.error('Create failed', msg)
      return null
    }
  }

  async function fetchImageDefinitionsWithBuilds(): Promise<void> {
    error.value = null
    try {
      const defs = await workspacesApi.listImageDefinitions()
      imageDefinitions.value = defs

      const pairs = await Promise.all(
        defs.map(async (definition) => {
          const builds = await workspacesApi.listRunnerImageBuilds(definition.id)
          return [definition.id, builds] as const
        }),
      )

      runnerBuildsByDefinition.value = Object.fromEntries(pairs)
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to load image definitions'
    }
  }

  return {
    capturedImages,
    imageDefinitions,
    runnerBuildsByDefinition,
    loading,
    error,
    fetchCapturedImages,
    renameCapturedImage,
    createWorkspaceFromCapturedImage,
    fetchImageDefinitionsWithBuilds,
  }
})
