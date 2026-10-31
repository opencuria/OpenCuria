/** Layout engine shared with the mobile Git graph; the palette is web-specific. */
export * from '@opencuria/chat-core/gitGraph'

/** Theme-aware branch colours (`--git-branch-1` … `--git-branch-8`). */
export const GIT_GRAPH_COLORS: string[] = [
  'var(--git-branch-1)',
  'var(--git-branch-2)',
  'var(--git-branch-3)',
  'var(--git-branch-4)',
  'var(--git-branch-5)',
  'var(--git-branch-6)',
  'var(--git-branch-7)',
  'var(--git-branch-8)',
]
