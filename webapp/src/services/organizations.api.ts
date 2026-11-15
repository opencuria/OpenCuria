/** Organizations REST API service. */
import { createOrganizationsApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export const {
  create: createOrganization,
  get: getOrganization,
  updateWorkspacePolicy: updateOrganizationWorkspacePolicy,
} = createOrganizationsApi(getApiClient)
