/**
 * Runner REST API service.
 */

import type { Runner, RunnerCreateIn, RunnerCreateOut, RunnerSystemMetrics, RunnerUpdateIn } from '@/types'
import { get, patch, post } from './api'

export function listRunners(): Promise<Runner[]> {
  return get<Runner[]>('/runners/')
}

export function getRunner(id: string): Promise<Runner> {
  return get<Runner>(`/runners/${id}/`)
}

export function createRunner(data: RunnerCreateIn): Promise<RunnerCreateOut> {
  return post<RunnerCreateOut>('/runners/', data)
}

export function getRunnerMetricsLatest(id: string): Promise<RunnerSystemMetrics> {
  return get<RunnerSystemMetrics>(`/runners/${id}/metrics/latest/`)
}

export function getRunnerMetricsHistory(id: string, hours = 24): Promise<RunnerSystemMetrics[]> {
  return get<RunnerSystemMetrics[]>(`/runners/${id}/metrics/history/?hours=${hours}`)
}

export function updateRunner(id: string, data: RunnerUpdateIn): Promise<Runner> {
  return patch<Runner>(`/runners/${id}/`, data)
}
