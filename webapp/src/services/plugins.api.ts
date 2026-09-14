/**
 * Plugins REST API service.
 *
 * Mirrors `backend/apps/plugins/api.py`:
 * - `/plugins/` catalog (list/get/create/update/delete/activation)
 * - `/workspaces/{id}/plugins/` workspace activation (list/replace)
 */

import type {
  Plugin,
  PluginActivationIn,
  PluginCreateIn,
  PluginUpdateIn,
  WorkspacePlugin,
  WorkspacePluginsUpdateIn,
} from '@/types'
import { del, get, patch, post, put } from './api'

export function listPlugins(): Promise<Plugin[]> {
  return get<Plugin[]>('/plugins/')
}

export function getPlugin(id: string): Promise<Plugin> {
  return get<Plugin>(`/plugins/${id}/`)
}

export function createPlugin(data: PluginCreateIn): Promise<Plugin> {
  return post<Plugin>('/plugins/', data)
}

export function updatePlugin(id: string, data: PluginUpdateIn): Promise<Plugin> {
  return patch<Plugin>(`/plugins/${id}/`, data)
}

export function deletePlugin(id: string): Promise<void> {
  return del<void>(`/plugins/${id}/`)
}

export function togglePluginActivation(id: string, active: boolean): Promise<Plugin> {
  const payload: PluginActivationIn = { active }
  return post<Plugin>(`/plugins/${id}/activation/`, payload)
}

export function listWorkspacePlugins(workspaceId: string): Promise<WorkspacePlugin[]> {
  return get<WorkspacePlugin[]>(`/workspaces/${workspaceId}/plugins/`)
}

export function updateWorkspacePlugins(
  workspaceId: string,
  data: WorkspacePluginsUpdateIn,
): Promise<WorkspacePlugin[]> {
  return put<WorkspacePlugin[]>(`/workspaces/${workspaceId}/plugins/`, data)
}
