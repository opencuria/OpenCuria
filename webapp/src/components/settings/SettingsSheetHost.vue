<!--
  SettingsSheetHost — Globales Settings-Sheet (Schritt 5).

  EINMAL in AppLayout eingebunden: öffnet das Sheet von überall
  (Sidebar-UserMenu feuert `opencuria:open-settings`), inkl.
  Deep-Link `/?settings=<tab>` (Query wird nach dem Öffnen entfernt).

  Genau EINE Behandlung pro Query: nur der Watcher auf
  `route.query.settings` löst aus (plus Mount für eine bereits vorhandene
  Query, z. B. nach Router-Redirect `/org-settings?tab=…` → `/?settings=…`).
  Ein `router.afterEach`-Hook darf hier NICHT zusätzlich ersetzen: Sein
  `router.replace` würde die Push-Navigation des auslösenden Links abbrechen
  und jede Navigation erneut durch den Host jagen
  (gemeldeter "Page Unresponsive"-Hänger beim Banner-Klick).
-->
<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SettingsSheet from './SettingsSheet.vue'
import {
  OPEN_SETTINGS_EVENT,
  SETTINGS_QUERY_PARAM,
  resolveSettingsTab,
} from './settingsTabs'

const route = useRoute()
const router = useRouter()

/** Zuletzt verarbeiteter Query-Wert: schützt vor Doppel-Events (Mount + Watcher
 *  derselben Navigation). Wird zurückgesetzt, sobald die Query weg ist, damit
 *  eine spätere neue Navigation mit demselben Wert erneut behandelt wird. */
let lastHandled: string | null = null
/** Läuft gerade das Query-Entfernen (replace)? Dann Watcher-Feuer ignorieren. */
let stripping = false

function openFromQuery(): boolean {
  const raw = route.query[SETTINGS_QUERY_PARAM]
  if (typeof raw !== 'string' || !raw) return false
  if (stripping || raw === lastHandled) return false
  lastHandled = raw
  const tab = resolveSettingsTab(raw)
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab } }))
  const nextQuery = { ...route.query }
  delete nextQuery[SETTINGS_QUERY_PARAM]
  stripping = true
  void router
    .replace({ path: route.path, query: nextQuery })
    .catch(() => undefined)
    .finally(() => {
      stripping = false
    })
  return true
}

watch(
  () => route.query[SETTINGS_QUERY_PARAM],
  (value) => {
    if (typeof value === 'string' && value) {
      openFromQuery()
    } else {
      // Query gestrippt/weg: Guard zurücksetzen, damit eine spätere, neue
      // Navigation mit demselben Wert erneut behandelt wird.
      lastHandled = null
    }
  },
)

onMounted(() => {
  // Bereits vorhandene ?settings=…-Query beim Mount verarbeiten
  // (z. B. nach Router-Redirect von /org-settings?tab=… → /?settings=…).
  openFromQuery()
})
</script>

<template>
  <SettingsSheet />
</template>
