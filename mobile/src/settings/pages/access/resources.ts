import type { OpenCuriaClient } from "@opencuria/client";

/** Resource keys shared by the access pages. */
export const CREDENTIALS = "credentials";
export const ACTIVE_SERVICES = "credential-services:available";
export const ORGANIZATION_SERVICES = "credential-services:organization";
export const PLUGINS = "plugins";
export const API_KEYS = "api-keys";
export const API_KEY_PERMISSIONS = "api-key-permissions";

export const loadServices = (client: OpenCuriaClient) =>
  client.credentials.listServices();
