/** Skills REST API service. */
import { createSkillsApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export const {
  list: listSkills,
  create: createSkill,
  update: updateSkill,
  delete: deleteSkill,
} = createSkillsApi(getApiClient)
