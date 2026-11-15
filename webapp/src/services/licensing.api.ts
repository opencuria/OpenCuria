/** Windows licensing REST API service (organization admins only). */
import { createWindowsLicensingApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

export const {
  get: getWindowsLicensing,
  update: updateWindowsLicensing,
  addKey: addWindowsProductKey,
  updateKey: updateWindowsProductKey,
  deleteKey: deleteWindowsProductKey,
  setUserLicensed: setWindowsUserLicensed,
} = createWindowsLicensingApi(getApiClient)
