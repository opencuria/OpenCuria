/** Skills REST API service: the skills of the plugins the user can use. */
import { createSkillsApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export const { list: listSkills } = createSkillsApi(getApiClient)
