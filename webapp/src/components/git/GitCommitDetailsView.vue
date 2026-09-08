<script setup lang="ts">
/**
 * GitCommitDetailsView — inline commit details below an expanded graph row.
 *
 * Stacked layout: summary (subject, meta, stats) on top, file list below.
 * Clicking a file selects it for diff in the main area. Height is resizable
 * and persisted through the store (cdvHeight).
 */
import { computed, ref } from 'vue'
import type { GitCommitFile } from '@/types/git'
import { useGitStore } from '@/stores/git'
import { useNotificationStore } from '@/stores/notifications'
import { formatDate } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  ChevronDown,
  ChevronRight,
  Copy,
  FileCode2,
  FileText,
  Folder,
  FolderOpen,
  FolderTree,
  List,
  X,
} from '@lucide/vue'

const props = defineProps<{
  hash: string
}>()

const store = useGitStore()
const notifications = useNotificationStore()

const details = computed(() =>
  store.expandedCommitHash === props.hash ? store.expandedCommitDetails : null,
)
const commit = computed(
  () => store.currentRepo?.commits.find((c) => c.hash === props.hash) ?? null,
)
const files = computed<GitCommitFile[]>(() => details.value?.fileChanges ?? [])

const totalAdditions = computed(() =>
  files.value.reduce((sum, f) => sum + f.additions, 0),
)
const totalDeletions = computed(() =>
  files.value.reduce((sum, f) => sum + f.deletions, 0),
)

// --- File view type (tree / list, persisted locally) ---

const VIEW_TYPE_KEY = 'opencuria:git:cdvViewType'
function loadViewType(): 'tree' | 'list' {
  try {
    return localStorage.getItem(VIEW_TYPE_KEY) === 'list' ? 'list' : 'tree'
  } catch {
    return 'tree'
  }
}
const viewType = ref<'tree' | 'list'>(loadViewType())
function setViewType(v: 'tree' | 'list'): void {
  viewType.value = v
  try {
    localStorage.setItem(VIEW_TYPE_KEY, v)
  } catch {
    // Non-fatal.
  }
}

// --- File tree (compacted folders, collapsible) ---

interface TreeFolderNode {
  kind: 'folder'
  name: string
  fullPath: string
  children: TreeNode[]
}
interface TreeFileNode {
  kind: 'file'
  name: string
  fullPath: string
  file: GitCommitFile
}
type TreeNode = TreeFolderNode | TreeFileNode

interface FlatRow {
  kind: 'folder' | 'file'
  name: string
  fullPath: string
  depth: number
  open: boolean
  file: GitCommitFile | null
}

function buildTree(commitFiles: GitCommitFile[]): TreeFolderNode {
  const root: TreeFolderNode = { kind: 'folder', name: '', fullPath: '', children: [] }
  for (const file of commitFiles) {
    const parts = file.newPath.split('/').filter((p) => p.length > 0)
    let parent = root
    let prefix = ''
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i] as string
      prefix = prefix ? `${prefix}/${part}` : part
      const last = i === parts.length - 1
      if (last) {
        parent.children.push({ kind: 'file', name: part, fullPath: prefix, file })
      } else {
        let folder = parent.children.find(
          (c): c is TreeFolderNode => c.kind === 'folder' && c.name === part,
        )
        if (!folder) {
          folder = { kind: 'folder', name: part, fullPath: prefix, children: [] }
          parent.children.push(folder)
        }
        parent = folder
      }
    }
  }
  return root
}

/** Merge folders with a single folder child (a/b compaction). */
function compactNodes(nodes: TreeNode[]): TreeNode[] {
  return nodes.map((node) => {
    if (node.kind !== 'folder') return node
    let current = node
    while (
      current.children.length === 1 &&
      current.children[0]!.kind === 'folder'
    ) {
      const child = current.children[0] as TreeFolderNode
      current = {
        kind: 'folder',
        name: `${current.name}/${child.name}`,
        fullPath: child.fullPath,
        children: child.children,
      }
    }
    return { ...current, children: compactNodes(current.children) }
  })
}

function sortNodes(nodes: TreeNode[]): TreeNode[] {
  return [...nodes]
    .sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'folder' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    .map((node) =>
      node.kind === 'folder'
        ? { ...node, children: sortNodes(node.children) }
        : node,
    )
}

/** Full paths of collapsed folders. */
const closedFolders = ref<Set<string>>(new Set())

function toggleFolder(fullPath: string): void {
  const next = new Set(closedFolders.value)
  if (next.has(fullPath)) {
    next.delete(fullPath)
  } else {
    next.add(fullPath)
  }
  closedFolders.value = next
}

const treeRows = computed<FlatRow[]>(() => {
  const rows: FlatRow[] = []
  const walk = (nodes: TreeNode[], depth: number): void => {
    for (const node of nodes) {
      if (node.kind === 'folder') {
        const open = !closedFolders.value.has(node.fullPath)
        rows.push({
          kind: 'folder',
          name: node.name,
          fullPath: node.fullPath,
          depth,
          open,
          file: null,
        })
        if (open) walk(node.children, depth + 1)
      } else {
        rows.push({
          kind: 'file',
          name: node.name,
          fullPath: node.fullPath,
          depth,
          open: true,
          file: node.file,
        })
      }
    }
  }
  walk(sortNodes(compactNodes(buildTree(files.value).children)), 0)
  return rows
})

const listRows = computed<GitCommitFile[]>(() =>
  [...files.value].sort((a, b) => a.newPath.localeCompare(b.newPath)),
)

// --- File row helpers ---

const CODE_EXTENSIONS = new Set([
  'js', 'ts', 'jsx', 'tsx', 'vue', 'py', 'go', 'rs', 'java', 'c', 'h', 'cpp',
  'hpp', 'cs', 'rb', 'php', 'swift', 'kt', 'sh', 'sql', 'html', 'css',
  'scss', 'json', 'yaml', 'yml', 'toml', 'xml', 'md',
])

function fileIcon(path: string) {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return CODE_EXTENSIONS.has(ext) ? FileCode2 : FileText
}

function fileName(path: string): string {
  return path.split('/').pop() ?? path
}

function dirName(path: string): string {
  const parts = path.split('/')
  return parts.length > 1 ? parts.slice(0, -1).join('/') : ''
}

const STATUS_LABELS: Record<GitCommitFile['status'], string> = {
  M: 'Modified',
  A: 'Added',
  D: 'Deleted',
  R: 'Renamed',
  U: 'Untracked',
}

const STATUS_COLORS: Record<GitCommitFile['status'], string> = {
  M: 'text-warning',
  A: 'text-success',
  D: 'text-error',
  R: 'text-purple-500',
  U: 'text-muted-foreground',
}

function isSelected(file: GitCommitFile): boolean {
  return (
    store.expandedFilePath === file.newPath ||
    store.expandedFilePath === file.oldPath
  )
}

function selectFile(file: GitCommitFile): void {
  if (isSelected(file)) {
    store.selectCommitFile(null)
  } else {
    store.selectCommitFile(file.newPath)
  }
}

function copyPath(path: string): void {
  navigator.clipboard.writeText(path)
  notifications.info('Copied file path', path)
}

function copyHash(): void {
  const value = details.value?.hash ?? props.hash
  navigator.clipboard.writeText(value)
  notifications.info('Copied commit hash', value)
}

// --- Height resize (persisted via the store) ---

const isResizing = ref(false)
let resizeStartY = 0
let resizeStartHeight = 0

function onResizeDown(event: PointerEvent): void {
  event.preventDefault()
  isResizing.value = true
  resizeStartY = event.clientY
  resizeStartHeight = store.cdvHeight
  const handle = event.currentTarget as HTMLElement
  handle.setPointerCapture?.(event.pointerId)
}

function onResizeMove(event: PointerEvent): void {
  if (!isResizing.value) return
  store.setCdvHeight(resizeStartHeight + (event.clientY - resizeStartY))
}

function onResizeUp(event: PointerEvent): void {
  if (!isResizing.value) return
  isResizing.value = false
  const handle = event.currentTarget as HTMLElement
  if (handle.hasPointerCapture?.(event.pointerId)) {
    handle.releasePointerCapture(event.pointerId)
  }
}
</script>

<template>
  <div
    class="flex flex-col"
    :style="{ height: `${store.cdvHeight}px` }"
    data-testid="git-commit-details"
  >
    <div
      class="min-h-0 max-h-[45%] shrink-0 overflow-auto border-b border-border p-2"
      data-testid="git-cdv-summary"
    >
      <div
        v-if="commit?.message"
        class="text-xs font-medium text-foreground"
        data-testid="git-cdv-subject"
      >
        {{ commit.message }}
      </div>
      <dl class="mt-1.5 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-[11px] leading-4">
        <dt class="font-medium text-muted-foreground">Commit</dt>
        <dd class="flex min-w-0 items-center gap-1" data-testid="git-cdv-hash">
          <span
            class="min-w-0 truncate font-mono text-foreground"
            :title="details?.hash ?? hash"
          >
            {{ details?.hash ?? hash }}
          </span>
          <Button
            variant="ghost"
            size="icon-sm"
            class="h-5 w-5 shrink-0"
            title="Copy commit hash"
            data-testid="git-cdv-copy-hash"
            @click="copyHash"
          >
            <Copy :size="11" />
          </Button>
        </dd>
        <dt class="font-medium text-muted-foreground">Parents</dt>
        <dd class="min-w-0 truncate" data-testid="git-cdv-parents">
          <span v-if="(details?.parents ?? []).length === 0" class="text-muted-foreground">
            None
          </span>
          <template v-else>
            <button
              v-for="(parent, index) in details?.parents ?? []"
              :key="parent"
              type="button"
              class="cursor-pointer font-mono text-foreground hover:underline"
              :data-testid="`git-cdv-parent-${parent}`"
              :title="`Show commit ${parent}`"
              @click="store.toggleCommitDetails(parent)"
            >
              {{ parent.slice(0, 7) }}<span v-if="index < (details?.parents.length ?? 0) - 1">, </span>
            </button>
          </template>
        </dd>
        <dt class="font-medium text-muted-foreground">Author</dt>
        <dd
          class="min-w-0 truncate text-foreground"
          :title="details ? `${details.author} <${details.authorEmail}>` : ''"
        >
          {{ details?.author }}
          <span v-if="details?.authorEmail" class="text-muted-foreground">
            &lt;{{ details.authorEmail }}&gt;
          </span>
        </dd>
        <dt class="font-medium text-muted-foreground">Date</dt>
        <dd
          class="min-w-0 truncate text-foreground"
          :title="details?.authorDate ?? ''"
        >
          {{ details ? formatDate(details.authorDate) : '' }}
        </dd>
        <template v-if="details && details.committerDate !== details.authorDate">
          <dt class="font-medium text-muted-foreground">Committer</dt>
          <dd class="min-w-0 truncate text-foreground">
            {{ details.committer }}
            <span v-if="details.committerEmail" class="text-muted-foreground">
              &lt;{{ details.committerEmail }}&gt;
            </span>
            · {{ formatDate(details.committerDate) }}
          </dd>
        </template>
      </dl>
      <p
        v-if="details?.body"
        class="mt-1.5 whitespace-pre-wrap text-[11px] text-muted-foreground"
        data-testid="git-cdv-body"
      >
        {{ details.body }}
      </p>
      <p class="mt-1.5 text-[11px] text-muted-foreground">
        <span class="text-success">+{{ totalAdditions }}</span>
        {{ ' ' }}
        <span class="text-error">−{{ totalDeletions }}</span>
        {{ ' ' }}· {{ files.length }} file{{ files.length === 1 ? '' : 's' }}
      </p>
    </div>

    <div class="flex min-h-0 flex-1 flex-col" data-testid="git-cdv-files">
      <div class="flex shrink-0 items-center gap-1 px-2 py-1">
        <span class="truncate text-[11px] font-medium text-foreground">
          Files ({{ files.length }})
        </span>
        <span class="flex-1" />
        <Button
          variant="ghost"
          size="icon-sm"
          class="h-5 w-5"
          :class="{ 'bg-muted': viewType === 'tree' }"
          title="File tree view"
          data-testid="git-cdv-view-tree"
          @click="setViewType('tree')"
        >
          <FolderTree :size="12" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          class="h-5 w-5"
          :class="{ 'bg-muted': viewType === 'list' }"
          title="File list view"
          data-testid="git-cdv-view-list"
          @click="setViewType('list')"
        >
          <List :size="12" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          class="h-5 w-5"
          title="Close commit details"
          data-testid="git-cdv-close"
          @click="store.closeCommitDetails()"
        >
          <X :size="12" />
        </Button>
      </div>
      <ScrollArea class="min-h-0 flex-1">
        <div v-if="files.length === 0" class="px-2 py-1 text-[11px] text-muted-foreground">
          No files changed in this commit.
        </div>
        <div v-else-if="viewType === 'tree'" class="p-1">
          <div
            v-for="row in treeRows"
            :key="`${row.kind}-${row.fullPath}`"
          >
            <div
              v-if="row.kind === 'folder'"
              class="flex cursor-pointer items-center gap-1 rounded-md px-1.5 py-1 hover:bg-muted"
              :style="{ paddingLeft: `${6 + row.depth * 14}px` }"
              :data-testid="`git-cdv-folder-${row.fullPath}`"
              @click="toggleFolder(row.fullPath)"
            >
              <component
                :is="row.open ? ChevronDown : ChevronRight"
                :size="12"
                class="shrink-0 text-muted-foreground"
              />
              <component
                :is="row.open ? FolderOpen : Folder"
                :size="12"
                class="shrink-0 text-muted-foreground"
              />
              <span class="min-w-0 truncate text-xs text-foreground">{{ row.name }}</span>
            </div>
            <div
              v-else-if="row.file"
              class="group flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-muted"
              :class="{ 'bg-muted': isSelected(row.file) }"
              :style="{ paddingLeft: `${6 + row.depth * 14}px` }"
              :data-testid="`git-cdv-file-${row.file.newPath}`"
              :title="row.file.oldPath !== row.file.newPath ? `${row.file.oldPath} → ${row.file.newPath}` : row.file.newPath"
              @click="selectFile(row.file)"
            >
              <component
                :is="fileIcon(row.file.newPath)"
                :size="13"
                class="shrink-0 text-muted-foreground"
              />
              <span class="min-w-0 flex-1 truncate text-xs text-foreground">
                {{ fileName(row.file.newPath) }}
                <span class="text-muted-foreground">{{ dirName(row.file.newPath) }}</span>
              </span>
              <span v-if="row.file.additions > 0" class="shrink-0 text-[10px] text-success">
                +{{ row.file.additions }}
              </span>
              <span v-if="row.file.deletions > 0" class="shrink-0 text-[10px] text-error">
                −{{ row.file.deletions }}
              </span>
              <Button
                variant="ghost"
                size="icon-sm"
                class="h-5 w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                title="Copy file path"
                :data-testid="`git-cdv-copy-${row.file.newPath}`"
                @click.stop="copyPath(row.file.newPath)"
              >
                <Copy :size="11" />
              </Button>
              <span
                class="w-3 shrink-0 text-center text-[10px] font-semibold"
                :class="STATUS_COLORS[row.file.status]"
                :title="STATUS_LABELS[row.file.status]"
              >
                {{ row.file.status }}
              </span>
            </div>
          </div>
        </div>
        <div v-else class="p-1">
          <div
            v-for="file in listRows"
            :key="file.newPath"
            class="group flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-muted"
            :class="{ 'bg-muted': isSelected(file) }"
            :data-testid="`git-cdv-file-${file.newPath}`"
            :title="file.oldPath !== file.newPath ? `${file.oldPath} → ${file.newPath}` : file.newPath"
            @click="selectFile(file)"
          >
            <component
              :is="fileIcon(file.newPath)"
              :size="13"
              class="shrink-0 text-muted-foreground"
            />
            <span class="min-w-0 flex-1 truncate text-xs text-foreground">
              {{ fileName(file.newPath) }}
              <span class="text-muted-foreground">{{ dirName(file.newPath) }}</span>
            </span>
            <span v-if="file.additions > 0" class="shrink-0 text-[10px] text-success">
              +{{ file.additions }}
            </span>
            <span v-if="file.deletions > 0" class="shrink-0 text-[10px] text-error">
              −{{ file.deletions }}
            </span>
            <Button
              variant="ghost"
              size="icon-sm"
              class="h-5 w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
              title="Copy file path"
              :data-testid="`git-cdv-copy-${file.newPath}`"
              @click.stop="copyPath(file.newPath)"
            >
              <Copy :size="11" />
            </Button>
            <span
              class="w-3 shrink-0 text-center text-[10px] font-semibold"
              :class="STATUS_COLORS[file.status]"
              :title="STATUS_LABELS[file.status]"
            >
              {{ file.status }}
            </span>
          </div>
        </div>
      </ScrollArea>
    </div>

    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize commit details"
      title="Drag to resize"
      class="group relative h-1.5 shrink-0 cursor-row-resize touch-none"
      :class="{ 'select-none': isResizing }"
      data-testid="git-cdv-resize"
      @pointerdown="onResizeDown"
      @pointermove="onResizeMove"
      @pointerup="onResizeUp"
      @pointercancel="onResizeUp"
    >
      <div
        class="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border transition-colors group-hover:bg-primary"
        :class="{ 'bg-primary': isResizing }"
      />
    </div>
  </div>
</template>
