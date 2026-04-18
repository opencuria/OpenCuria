import type { Credential } from '@/types'

export interface CredentialServiceGroup {
  serviceId: string
  serviceName: string
  credentialType: string
  envVarName: string
  targetPath: string
  selectedCredentialId: string | null
  credentials: Credential[]
}

export function toggleWorkspaceCredentialSelection(
  selectedIds: string[],
  nextCredential: Pick<Credential, 'id' | 'service_id'>,
  allCredentials: Array<Pick<Credential, 'id' | 'service_id'>>,
): string[] {
  if (selectedIds.includes(nextCredential.id)) {
    return selectedIds.filter((id) => id !== nextCredential.id)
  }

  const credentialsById = new Map(allCredentials.map((credential) => [credential.id, credential]))
  const withoutSameService = selectedIds.filter(
    (id) => credentialsById.get(id)?.service_id !== nextCredential.service_id,
  )

  return [...withoutSameService, nextCredential.id]
}

export function groupWorkspaceCredentials(
  credentials: Credential[],
  selectedIds: string[],
): CredentialServiceGroup[] {
  const selectedSet = new Set(selectedIds)
  const groups = new Map<string, CredentialServiceGroup>()

  const sortedCredentials = [...credentials].sort((left, right) => {
    const serviceCompare = left.service_name.localeCompare(right.service_name)
    if (serviceCompare !== 0) return serviceCompare
    return left.name.localeCompare(right.name)
  })

  for (const credential of sortedCredentials) {
    const existingGroup = groups.get(credential.service_id)
    if (existingGroup) {
      existingGroup.credentials.push(credential)
      if (selectedSet.has(credential.id)) {
        existingGroup.selectedCredentialId = credential.id
      }
      continue
    }

    groups.set(credential.service_id, {
      serviceId: credential.service_id,
      serviceName: credential.service_name,
      credentialType: credential.credential_type,
      envVarName: credential.env_var_name,
      targetPath: credential.target_path,
      selectedCredentialId: selectedSet.has(credential.id) ? credential.id : null,
      credentials: [credential],
    })
  }

  return Array.from(groups.values())
}
