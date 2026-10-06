/**
 * Organizations REST API service.
 */

import type { Organization, OrganizationCreateIn } from '@/types'
import { get, patch, post } from './api'

export function listOrganizations(): Promise<Organization[]> {
  return get<Organization[]>('/organizations/')
}

export function createOrganization(data: OrganizationCreateIn): Promise<Organization> {
  return post<Organization>('/organizations/', data)
}

export function getOrganization(id: string): Promise<Organization> {
  return get<Organization>(`/organizations/${id}/`)
}

export function updateOrganizationWorkspacePolicy(
  id: string,
  data: {
    workspace_auto_stop_timeout_minutes?: number | null
    image_versions_to_keep?: number
  },
): Promise<Organization> {
  return patch<Organization>(`/organizations/${id}/workspace-policy/`, data)
}
