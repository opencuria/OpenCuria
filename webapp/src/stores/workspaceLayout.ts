import { computed, ref, type InjectionKey, type Ref } from 'vue'
import { defineStore } from 'pinia'
import { useWindowSize } from '@vueuse/core'
import { useSidePanelStore } from '@/stores/sidePanel'
import { DEFAULT_PANEL_WIDTH, MAX_PANEL_VIEWPORT_FRACTION, MIN_PANEL_WIDTH } from '@/lib/sidePanel'
import {
  CHAT_SIDE_STORAGE_KEY,
  COLLAPSED_NAVIGATION_WIDTH,
  DEFAULT_NAVIGATION_WIDTH,
  LEFT_CHAT_WIDTH_STORAGE_KEY,
  MIN_LEFT_CHAT_WIDTH,
  MIN_NAVIGATION_WIDTH,
  NAVIGATION_WIDTH_STORAGE_KEY,
  loadChatSide,
  loadSidebarWidth,
  maxSidebarWidth,
  saveLayoutPreference,
} from '@/lib/workspaceLayout'

export const sidebarChatTargetKey: InjectionKey<Ref<HTMLElement | null>> =
  Symbol('sidebarChatTarget')

export const useWorkspaceLayoutStore = defineStore('workspaceLayout', () => {
  const sidePanel = useSidePanelStore()
  const { width: viewportWidth } = useWindowSize({ listenOrientation: false })
  const isWideLayout = computed(() => viewportWidth.value >= 1024)
  const navigationOpen = ref(true)
  const sidebarChatOpen = ref(false)
  const rightPanelVisible = ref(false)
  const chatSide = ref(loadChatSide())
  const navigationWidth = ref(
    loadSidebarWidth(NAVIGATION_WIDTH_STORAGE_KEY, DEFAULT_NAVIGATION_WIDTH, MIN_NAVIGATION_WIDTH),
  )
  const leftChatWidth = ref(
    loadSidebarWidth(LEFT_CHAT_WIDTH_STORAGE_KEY, DEFAULT_PANEL_WIDTH, MIN_LEFT_CHAT_WIDTH),
  )
  const leftChatDocked = computed(
    () => isWideLayout.value && sidebarChatOpen.value && chatSide.value === 'left',
  )
  const leftMinimum = computed(() =>
    leftChatDocked.value ? MIN_LEFT_CHAT_WIDTH : MIN_NAVIGATION_WIDTH,
  )
  const leftWidth = computed(() => {
    if (viewportWidth.value <= 768) return 0
    if (!leftChatDocked.value && !navigationOpen.value) return COLLAPSED_NAVIGATION_WIDTH
    const preferred = leftChatDocked.value ? leftChatWidth.value : navigationWidth.value
    const reservedRight = isWideLayout.value && rightPanelVisible.value ? MIN_PANEL_WIDTH : 0
    return Math.min(
      preferred,
      maxSidebarWidth(viewportWidth.value, reservedRight, leftMinimum.value),
    )
  })
  const rightMaxWidth = computed(() =>
    Math.min(
      maxSidebarWidth(viewportWidth.value, leftWidth.value, MIN_PANEL_WIDTH),
      Math.max(MIN_PANEL_WIDTH, Math.floor(viewportWidth.value * MAX_PANEL_VIEWPORT_FRACTION)),
    ),
  )
  const rightWidth = computed(() => Math.min(sidePanel.width, rightMaxWidth.value))
  const leftMaxWidth = computed(() =>
    maxSidebarWidth(
      viewportWidth.value,
      isWideLayout.value && rightPanelVisible.value ? rightWidth.value : 0,
      leftMinimum.value,
    ),
  )

  function moveChat(): void {
    chatSide.value = chatSide.value === 'right' ? 'left' : 'right'
    saveLayoutPreference(CHAT_SIDE_STORAGE_KEY, chatSide.value)
  }

  function setLeftWidth(width: number): void {
    if (!Number.isFinite(width)) return
    const clamped = Math.round(Math.min(leftMaxWidth.value, Math.max(leftMinimum.value, width)))
    if (leftChatDocked.value) leftChatWidth.value = clamped
    else navigationWidth.value = clamped
  }

  function persistLeftWidth(): void {
    saveLayoutPreference(
      leftChatDocked.value ? LEFT_CHAT_WIDTH_STORAGE_KEY : NAVIGATION_WIDTH_STORAGE_KEY,
      leftChatDocked.value ? leftChatWidth.value : navigationWidth.value,
    )
  }

  function resetLeftWidth(): void {
    setLeftWidth(leftChatDocked.value ? DEFAULT_PANEL_WIDTH : DEFAULT_NAVIGATION_WIDTH)
    persistLeftWidth()
  }

  function setRightWidth(width: number): void {
    sidePanel.setWidth(Math.min(width, rightMaxWidth.value))
  }

  return {
    isWideLayout,
    navigationOpen,
    sidebarChatOpen,
    rightPanelVisible,
    chatSide,
    leftChatDocked,
    leftMinimum,
    leftWidth,
    leftMaxWidth,
    rightWidth,
    rightMaxWidth,
    moveChat,
    setLeftWidth,
    persistLeftWidth,
    resetLeftWidth,
    setRightWidth,
  }
})
