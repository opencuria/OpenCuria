import { computed, ref } from 'vue'
import {
  isDefinitionSelectableForWorkspace,
  workspaceImageOptions,
} from '@opencuria/chat-core/workspaceCreation'
import { useImageStore } from '@/stores/images'
import { useRunnerStore } from '@/stores/runners'
import { RunnerStatus } from '@/types'

/**
 * Whether a new workspace can start right now: runners online and images
 * ready on them. Shared by the first-run guide and the create dialog so both
 * judge availability the same way. Reads only member-visible lists.
 */
export function useWorkspaceImageReadiness() {
  const runnerStore = useRunnerStore()
  const imageStore = useImageStore()

  /** True after the first check settled; until then readiness is unknown. */
  const checked = ref(false)

  const imageOptions = computed(() =>
    workspaceImageOptions({
      definitions: imageStore.imageDefinitions,
      buildsByDefinition: imageStore.runnerBuildsByDefinition,
      capturedImages: imageStore.capturedImages,
      runners: runnerStore.runners,
    }),
  )
  const readyImages = computed(() => [
    ...imageOptions.value.definitions,
    ...imageOptions.value.captured,
  ])
  const onlineRunner = computed(
    () => runnerStore.runners.find((runner) => runner.status === RunnerStatus.ONLINE) ?? null,
  )
  const buildableDefinition = computed(
    () => imageStore.imageDefinitions.find(isDefinitionSelectableForWorkspace) ?? null,
  )
  /** A failed list makes readiness unknown rather than "nothing available". */
  const error = computed(
    () => runnerStore.error || imageStore.error || imageStore.definitionsError || null,
  )
  const ready = computed(() => readyImages.value.length > 0)

  async function check(): Promise<void> {
    await Promise.all([
      runnerStore.fetchRunners(),
      imageStore.fetchCapturedImages(),
      imageStore.fetchImageDefinitionsWithBuilds(),
    ])
    checked.value = true
  }

  return {
    checked,
    imageOptions,
    readyImages,
    onlineRunner,
    buildableDefinition,
    error,
    ready,
    check,
  }
}
