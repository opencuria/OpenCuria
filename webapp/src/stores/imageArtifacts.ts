import { defineStore } from 'pinia'
import { computed } from 'vue'
import { useImageStore } from './images'

export const useImageArtifactStore = defineStore('image-artifacts', () => {
  const images = useImageStore()

  return {
    imageArtifacts: computed(() => images.images),
    loading: computed(() => images.loading),
    error: computed(() => images.error),
    fetchImageArtifacts: images.fetchImages,
    createImageArtifact: images.createImageArtifact,
    renameImageArtifact: images.renameImageArtifact,
    deleteImageArtifact: images.deleteImageArtifact,
    createWorkspaceFromImageArtifact: images.createWorkspaceFromImageArtifact,
  }
})
