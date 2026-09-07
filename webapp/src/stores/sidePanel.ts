/**
 * Side panel store — manages the workspace side panel state.
 *
 * The side panel docks to the right of the workspace chat and hosts the
 * Git, Desktop, Terminal and Files tabs. Visibility, active tab and width
 * are persisted to localStorage.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  DEFAULT_PANEL_WIDTH,
  clampPanelWidth,
  loadPanelState,
  loadPanelWidth,
  savePanelState,
  savePanelWidth,
  type SidePanelTab,
} from '@/lib/sidePanel'
import { useTerminalStore } from '@/stores/terminal'

export const useSidePanelStore = defineStore('sidePanel', () => {
  const persisted = loadPanelState()

  const isOpen = ref(persisted.isOpen)
  const activeTab = ref<SidePanelTab>(persisted.activeTab)
  const width = ref(loadPanelWidth())
  /** True once the panel was opened at least once (drives lazy mounting). */
  const hasOpened = ref(persisted.isOpen)

  function persistState(): void {
    savePanelState({ isOpen: isOpen.value, activeTab: activeTab.value })
  }

  function toggle(): void {
    isOpen.value = !isOpen.value
    if (isOpen.value) hasOpened.value = true
    persistState()
  }

  function open(tab?: SidePanelTab): void {
    if (tab) activeTab.value = tab
    isOpen.value = true
    hasOpened.value = true
    if (activeTab.value === 'terminal') useTerminalStore().open()
    persistState()
  }

  function close(): void {
    isOpen.value = false
    persistState()
  }

  function setTab(tab: SidePanelTab): void {
    activeTab.value = tab
    if (tab === 'terminal') useTerminalStore().open()
    persistState()
  }

  function setWidth(value: number): void {
    width.value = clampPanelWidth(value, window.innerWidth)
  }

  function persistWidth(): void {
    savePanelWidth(width.value)
  }

  function resetWidth(): void {
    width.value = DEFAULT_PANEL_WIDTH
    savePanelWidth(width.value)
  }

  return {
    isOpen,
    activeTab,
    width,
    hasOpened,
    toggle,
    open,
    close,
    setTab,
    setWidth,
    persistWidth,
    resetWidth,
  }
})
