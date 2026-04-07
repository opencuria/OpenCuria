/**
 * Skills REST API service.
 */

import type { Skill, SkillCreateIn, SkillUpdateIn } from '@/types'
import { get, post, patch, del } from './api'

export function listSkills(): Promise<Skill[]> {
  return get<Skill[]>('/skills/')
}

export function createSkill(data: SkillCreateIn): Promise<Skill> {
  return post<Skill>('/skills/', data)
}

export function updateSkill(id: string, data: SkillUpdateIn): Promise<Skill> {
  return patch<Skill>(`/skills/${id}/`, data)
}

export function deleteSkill(id: string): Promise<void> {
  return del<void>(`/skills/${id}/`)
}
