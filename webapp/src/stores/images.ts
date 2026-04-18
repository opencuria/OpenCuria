/**
 * Pinia store for managing workspace images.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'
import type {
  ImageArtifact,
  ImageArtifactCreateIn,
  ImageArtifactCloneIn,
  ImageDefinition,
  RunnerImageBuild,
} from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { useNotificationStore } from './notifications'

export const useImageStore = defineStore('images', () => {
  const notifications = useNotificationStore()

  const images = ref<ImageArtifact[]>([])
  const imageDefinitions = ref<ImageDefinition[]>([])
  const runnerBuildsByDefinition = ref<Record<string, RunnerImageBuild[]>>({})
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchImages(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      images.value = await workspacesApi.listUserImageArtifacts()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to load images'
    } finally {
      loading.value = false
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

  async function createImageArtifact(data: ImageArtifactCreateIn): Promise<boolean> {
    try {
      await workspacesApi.createImageArtifact(data)
      notifications.success('Image creating', 'Image is being created. It will appear here when ready.')
      return true
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to create image'
      notifications.error('Image failed', msg)
      return false
    }
  }

  async function deleteImageArtifact(imageArtifactId: string): Promise<boolean> {
    try {
      await workspacesApi.deleteImageArtifact(imageArtifactId)
      // Mark locally as deleting (will be confirmed via next fetch)
      const idx = images.value.findIndex((a) => a.id === imageArtifactId)
      if (idx !== -1) {
        images.value[idx] = { ...images.value[idx], status: 'deleting' }
      }
      notifications.success('Delete initiated', 'Image deletion has been initiated.')
      return true
    } catch (e: any) {
      if (e?.response?.status === 409) {
        const detail = e?.response?.data?.detail || 'Image is in use and cannot be deleted.'
        notifications.error('Cannot delete', detail)
      } else {
        const msg = e instanceof Error ? e.message : 'Failed to delete image'
        notifications.error('Delete failed', msg)
      }
      return false
    }
  }

  async function renameImageArtifact(imageArtifactId: string, name: string): Promise<boolean> {
    try {
      const updated = await workspacesApi.renameImageArtifact(imageArtifactId, name)
      const idx = images.value.findIndex((artifact) => artifact.id === imageArtifactId)
      if (idx !== -1) images.value[idx] = updated
      notifications.success('Image renamed', `Image renamed to "${name}".`)
      return true
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to rename image'
      notifications.error('Rename failed', msg)
      return false
    }
  }

  async function createWorkspaceFromImageArtifact(
    imageArtifactId: string,
    data: ImageArtifactCloneIn,
  ): Promise<string | null> {
    try {
      const result = await workspacesApi.createWorkspaceFromUserImageArtifact(
        imageArtifactId,
        data,
      )
      notifications.success('Cloning workspace', 'New workspace is being created from image.')
      return result.workspace_id
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to clone workspace'
      notifications.error('Clone failed', msg)
      return null
    }
  }

  return {
    images,
    imageDefinitions,
    runnerBuildsByDefinition,
    loading,
    error,
    fetchImages,
    fetchImageDefinitionsWithBuilds,
    createImageArtifact,
    renameImageArtifact,
    deleteImageArtifact,
    createWorkspaceFromImageArtifact,
  }
})
