/**
 * Skills Pinia store.
 *
 * The skills of the plugins the user can use, for the composer's `/` picker,
 * scheduled tasks and message pills. Skills are edited in their plugin.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import type { Skill } from '@/types'
import * as skillsApi from '@/services/skills.api'

export const useSkillStore = defineStore('skills', () => {
  const skills = ref<Skill[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchSkills(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      skills.value = await skillsApi.listSkills()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load skills'
    } finally {
      loading.value = false
    }
  }

  return { skills, loading, error, fetchSkills }
})
