import type { Credential } from '@/types'

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
