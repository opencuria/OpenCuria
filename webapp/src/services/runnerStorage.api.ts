/** Runner storage inventory, image deletions and lifecycle operation review. */
import { createDeletionsApi, createRunnersApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export const {
  storage: getRunnerStorage,
  refreshStorage: refreshRunnerStorage,
  inspectOperation,
  disposeOperation,
} = createRunnersApi(getApiClient)

export const {
  preview: previewDeletion,
  request: requestDeletion,
  list: listDeletions,
  cancel: cancelDeletion,
} = createDeletionsApi(getApiClient)
