import type { ImageArtifact, ImageVersionRef, ImageVersionRetention } from '@/types'

/** Select value for "capture as a new image" (instead of a new version). */
export const NEW_IMAGE = '__new__'

/** "name · v3" for the version a workspace is based on. */
export function versionLabel(ref: ImageVersionRef | null | undefined): string | null {
  if (!ref) return null
  return ref.version ? `${ref.name} · v${ref.version}` : ref.name
}

export function retentionLabel(version: ImageArtifact): string | null {
  if (version.status === 'failed') return 'Failed'
  if (['pending_deletion', 'deleting', 'delete_failed'].includes(version.status)) {
    return 'Deleting'
  }
  const labels: Record<ImageVersionRetention, string> = {
    latest: 'Latest',
    kept: 'Kept',
    expires_when_unused: 'Removed when unused',
  }
  return version.retention ? labels[version.retention] : null
}

/** Why a workspace cannot be reset/updated right now; null when it can. */
export function recreateBlocker(base: ImageVersionRef | null | undefined): string | null {
  if (!base) return 'This workspace predates image tracking and cannot be reset.'
  if (base.status !== 'ready' && !base.update_available) {
    return 'Its image version is being deleted. Delete the workspace instead.'
  }
  return null
}
