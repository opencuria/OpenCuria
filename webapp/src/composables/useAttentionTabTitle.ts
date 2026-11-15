import { onUnmounted, watch, type Ref } from 'vue'

const BASE_TITLE = 'OpenCuria'

/**
 * Prefix the document title with a count of chats waiting on user input.
 */
export function useAttentionTabTitle(count: Ref<number>): void {
  function apply(value: number): void {
    document.title = value > 0 ? `(${value}) ${BASE_TITLE}` : BASE_TITLE
  }

  apply(count.value)
  const stop = watch(count, (value) => {
    apply(value)
  })

  onUnmounted(() => {
    stop()
    document.title = BASE_TITLE
  })
}
