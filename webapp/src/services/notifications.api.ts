/** Organization push configuration contains vault references, never signing keys. */
import { get, put } from './api'

export interface PushConfiguration {
  ios: boolean
  android: boolean
  live_activities: boolean
  apns_credential_id: string | null
  fcm_credential_id: string | null
}

export function getPushConfiguration(): Promise<PushConfiguration> {
  return get('/notifications/configuration/')
}

export function savePushConfiguration(
  input: Pick<PushConfiguration, 'apns_credential_id' | 'fcm_credential_id'>,
): Promise<PushConfiguration> {
  return put('/notifications/configuration/', input)
}
