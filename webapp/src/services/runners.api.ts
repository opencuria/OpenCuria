/** Runner REST API service. */
import { createRunnersApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export const {
  list: listRunners,
  create: createRunner,
  update: updateRunner,
  latestMetrics: getRunnerMetricsLatest,
  metricsHistory: getRunnerMetricsHistory,
} = createRunnersApi(getApiClient)
