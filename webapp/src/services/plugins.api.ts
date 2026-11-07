/** Plugin catalog REST API (workspace selection updates use workspace APIs). */
import { createPluginsApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export const {
  list: listPlugins,
  create: createPlugin,
  update: updatePlugin,
  delete: deletePlugin,
  setActive: togglePluginActivation,
} = createPluginsApi(getApiClient)
