<!--
  SettingsSheetHost — Globales Settings-Sheet (Schritt 5).

  EINMAL in AppLayout eingebunden: öffnet das Sheet von überall
  (Sidebar-UserMenu feuert `opencuria:open-settings`), inkl.
  Deep-Link `/?settings=<tab>` (Query wird nach dem Öffnen entfernt).
-->
<script setup lang="ts">
import { onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SettingsSheet from './SettingsSheet.vue'
import {
  OPEN_SETTINGS_EVENT,
  SETTINGS_QUERY_PARAM,
  resolveSettingsTab,
} from './settingsTabs'

const route = useRoute()
const router = useRouter()

function openFromQuery(): boolean {
  const raw = route.query[SETTINGS_QUERY_PARAM]
  if (typeof raw !== 'string' || !raw) return false
  const tab = resolveSettingsTab(raw)
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab } }))
  const nextQuery = { ...route.query }
  delete nextQuery[SETTINGS_QUERY_PARAM]
  void router.replace({ path: route.path, query: nextQuery })
  return true
}

watch(
  () => route.query[SETTINGS_QUERY_PARAM],
  (value) => {
    if (typeof value === 'string' && value) openFromQuery()
  },
)

let removeGuard: (() => void) | null = null

onMounted(() => {
  // Bereits vorhandene ?settings=…-Query beim Mount verarbeiten
  // (z. B. nach Router-Redirect von /org-settings?tab=… → /?settings=…).
  openFromQuery()
  removeGuard = router.afterEach(() => {
    openFromQuery()
  })
})

onUnmounted(() => {
  removeGuard?.()
  removeGuard = null
})
</script>

<template>
  <SettingsSheet />
</template>
