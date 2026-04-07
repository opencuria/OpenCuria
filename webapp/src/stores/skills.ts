/**
 * Skills Pinia store.
 *
 * Manages personal and org-shared skills. Mirrors the pattern of credentials.ts.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import type { Skill, SkillCreateIn, SkillUpdateIn } from '@/types'
import * as skillsApi from '@/services/skills.api'
import { useNotificationStore } from './notifications'

export const useSkillStore = defineStore('skills', () => {
  // --- State ---
  const skills = ref<Skill[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // --- Actions ---

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

  async function createSkill(data: SkillCreateIn): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      const skill = await skillsApi.createSkill(data)
      skills.value = [skill, ...skills.value]
      notifications.success('Skill created', 'The skill has been saved.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create skill'
      notifications.error('Creation failed', msg)
      return false
    }
  }

  async function updateSkill(id: string, data: SkillUpdateIn): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      const updated = await skillsApi.updateSkill(id, data)
      const idx = skills.value.findIndex((s) => s.id === id)
      if (idx !== -1) skills.value[idx] = updated
      notifications.success('Skill updated', 'The skill has been updated.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update skill'
      notifications.error('Update failed', msg)
      return false
    }
  }

  async function deleteSkill(id: string): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      await skillsApi.deleteSkill(id)
      skills.value = skills.value.filter((s) => s.id !== id)
      notifications.success('Skill deleted', 'The skill has been removed.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to delete skill'
      notifications.error('Deletion failed', msg)
      return false
    }
  }

  return {
    skills,
    loading,
    error,
    fetchSkills,
    createSkill,
    updateSkill,
    deleteSkill,
  }
})
