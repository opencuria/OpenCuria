<script setup lang="ts">
/**
 * GitChangesSection — VSCode-style source control section of the Git tab.
 *
 * Commit message box with Commit / Commit & Push split button and a Push
 * button plus a compact remote-actions dropdown (Pull / Sync), above
 * collapsible "Staged Changes" and "Changes" groups. A merge banner shows
 * while a merge is in progress (with an abort confirmation dialog). File
 * rows offer hover actions (stage/unstage, discard) and open the working
 * diff in the main area when clicked (staged vs unstaged side aware).
 */
import { computed, ref } from 'vue'
import type { GitFileChange, GitFileStatus } from '@/types/git'
import { useGitStore } from '@/stores/git'
import { copyToClipboard } from '@/lib/clipboard'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  FileCode2,
  FileText,
  Minus,
  Plus,
  RefreshCw,
  Undo2,
  Upload,
} from '@lucide/vue'

const store = useGitStore()

const message = ref('')

const modKey = navigator.userAgent.includes('Mac') ? '⌘' : 'Ctrl'
const placeholder = computed(() => {
  const branch = store.currentRepo?.currentBranch
  const target = branch ? `'${branch}'` : 'detached HEAD'
  return `Message (${modKey}+Enter to commit on ${target})`
})

const canCommit = computed(
  () =>
    message.value.trim().length > 0 &&
    store.stagedChanges.length > 0 &&
    store.busyOperation === null,
)
const ahead = computed(() => store.currentBranch?.ahead ?? 0)
const behind = computed(() => store.currentBranch?.behind ?? 0)
const isBusy = computed(() => store.busyOperation !== null)
const isDetached = computed(() => store.currentRepo?.currentBranch === null)
const hasUpstream = computed(() => store.currentBranch?.upstream != null)
/** Push is enabled for a checked-out branch that is ahead or has never
 *  been published (no upstream yet — first push sets the upstream). */
const canPush = computed(
  () => !isBusy.value && !isDetached.value && (ahead.value > 0 || !hasUpstream.value),
)
const needsPublish = computed(() => !isDetached.value && !hasUpstream.value)
const pushTitle = computed(() => {
  if (isBusy.value) return 'A git operation is already running'
  if (isDetached.value) return 'Not available while HEAD is detached'
  if (needsPublish.value) return 'Publish the current branch to the remote'
  if (ahead.value === 0) return 'Everything up to date'
  return 'Push to origin'
})
const canRemoteAction = computed(() => !isBusy.value && !isDetached.value)
const remoteDisabledReason = computed(() => {
  if (isBusy.value) return 'A git operation is already running'
  if (isDetached.value) return 'Not available while HEAD is detached'
  return ''
})
/** Pull/Sync need a tracking branch: without an upstream git has nothing
 *  to fetch into / merge from, so the items stay disabled until publish. */
const canTrackRemote = computed(() => canRemoteAction.value && hasUpstream.value)
const trackDisabledReason = computed(() => {
  if (!canRemoteAction.value) return remoteDisabledReason.value
  if (!hasUpstream.value) return 'Publish branch first'
  return ''
})

const isMerging = computed(() => store.currentRepo?.mergeState.merging === true)
const conflictCount = computed(
  () => store.currentRepo?.changes.filter((c) => c.conflict !== null).length ?? 0,
)

async function doCommit(andPush: boolean): Promise<void> {
  if (!canCommit.value) return
  const ok = await store.commit(message.value, andPush)
  if (ok) message.value = ''
}

function onMessageKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault()
    void doCommit(false)
  }
}

// --- Remote actions (serialized through the store, no parallel double-clicks) ---

async function doPull(): Promise<void> {
  if (!canTrackRemote.value) return
  await store.pull()
}

async function doSync(): Promise<void> {
  if (!canTrackRemote.value) return
  await store.sync()
}

// --- Merge abort confirmation ---

const abortDialogOpen = ref(false)
const abortSubmitting = ref(false)

function openAbortDialog(): void {
  abortDialogOpen.value = true
}

async function confirmAbort(): Promise<void> {
  if (abortSubmitting.value) return
  abortSubmitting.value = true
  try {
    const ok = await store.mergeAbort()
    if (ok) abortDialogOpen.value = false
  } finally {
    abortSubmitting.value = false
  }
}

// --- Discard confirmation ---

const discardTarget = ref<string | null>(null)

function confirmDiscard(): void {
  if (discardTarget.value) void store.discard(discardTarget.value)
  discardTarget.value = null
}

// --- File row helpers ---

function copyPath(path: string): void {
  void copyToClipboard(path, 'file path')
}

const CODE_EXTENSIONS = new Set([
  'js', 'ts', 'jsx', 'tsx', 'vue', 'py', 'go', 'rs', 'java', 'c', 'h', 'cpp',
  'hpp', 'cs', 'rb', 'php', 'swift', 'kt', 'sh', 'sql', 'html', 'css',
  'scss', 'json', 'yaml', 'yml', 'toml', 'xml', 'md',
])

function fileIcon(change: GitFileChange) {
  const ext = change.path.split('.').pop()?.toLowerCase() ?? ''
  return CODE_EXTENSIONS.has(ext) ? FileCode2 : FileText
}

function fileName(path: string): string {
  return path.split('/').pop() ?? path
}

function dirName(path: string): string {
  const parts = path.split('/')
  return parts.length > 1 ? parts.slice(0, -1).join('/') : ''
}

const STATUS_COLORS: Record<GitFileStatus, string> = {
  M: 'text-warning',
  A: 'text-success',
  D: 'text-error',
  R: 'text-purple-500',
  C: 'text-purple-500',
  U: 'text-muted-foreground',
}

const STATUS_LABELS: Record<GitFileStatus, string> = {
  M: 'Modified',
  A: 'Added',
  D: 'Deleted',
  R: 'Renamed',
  C: 'Copied',
  U: 'Unresolved',
}

const stagedOpen = ref(true)
const changesOpen = ref(true)
</script>

<template>
  <div class="flex h-full flex-col" data-testid="git-changes-section">
    <!-- Commit box -->
    <div class="shrink-0 space-y-1.5 border-b border-border p-2">
      <Textarea
        v-model="message"
        :placeholder="placeholder"
        rows="2"
        class="max-h-24 min-h-14 resize-none text-xs"
        data-testid="git-commit-message"
        @keydown="onMessageKeydown"
      />
      <div class="flex items-center gap-1">
        <div class="flex min-w-0 flex-1">
          <Button
            size="sm"
            class="h-7 min-w-0 flex-1 gap-1.5 rounded-r-none text-xs"
            :disabled="!canCommit"
            data-testid="git-commit"
            @click="void doCommit(false)"
          >
            <Check :size="13" />
            <span class="truncate">Commit</span>
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger as-child>
              <Button
                size="sm"
                class="h-7 w-6 shrink-0 rounded-l-none border-l border-primary-foreground/20 px-0"
                :disabled="!canCommit"
                title="More commit actions"
                data-testid="git-commit-more"
              >
                <ChevronDown :size="12" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuItem @click="void doCommit(false)">
                Commit
              </DropdownMenuItem>
              <DropdownMenuItem
                data-testid="git-commit-push"
                @click="void doCommit(true)"
              >
                Commit &amp; Push
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div class="flex shrink-0 items-center gap-1">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger as-child>
                <span class="inline-flex">
                  <Button
                    variant="outline"
                    size="sm"
                    class="h-7 shrink-0 gap-1.5 rounded-r-none text-xs"
                    :disabled="!canPush"
                    :title="pushTitle"
                    data-testid="git-push"
                    @click="void store.push()"
                  >
                    <Upload :size="12" />
                    {{ needsPublish ? 'Publish' : 'Push' }}
                    <span v-if="ahead > 0" class="text-muted-foreground">{{ ahead }}</span>
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent v-if="!canPush && pushTitle" side="bottom">
                {{ pushTitle }}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <DropdownMenu>
            <DropdownMenuTrigger as-child>
              <span class="inline-flex">
                <Button
                  variant="outline"
                  size="sm"
                  class="h-7 w-7 shrink-0 rounded-l-none border-l-0 px-0"
                  :disabled="!canRemoteAction"
                  title="Remote actions: pull, sync"
                  data-testid="git-remote-actions"
                >
                  <ChevronDown :size="12" />
                </Button>
              </span>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                data-testid="git-pull"
                :disabled="!canTrackRemote"
                :title="trackDisabledReason || 'Fetch from the remote and merge into the current branch'"
                @click="void doPull()"
              >
                <ArrowDown :size="13" />
                Pull
                <span v-if="behind > 0" class="ml-auto text-muted-foreground">{{ behind }}</span>
              </DropdownMenuItem>
              <DropdownMenuItem
                data-testid="git-sync"
                :disabled="!canTrackRemote"
                :title="trackDisabledReason || 'Pull then push: fetch, merge, then push local commits'"
                @click="void doSync()"
              >
                <RefreshCw :size="13" />
                Sync
                <span v-if="ahead > 0 || behind > 0" class="ml-auto text-muted-foreground">
                  {{ behind > 0 ? `↓${behind}` : '' }}{{ ahead > 0 && behind > 0 ? ' ' : '' }}{{ ahead > 0 ? `↑${ahead}` : '' }}
                </span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </div>

    <!-- Merge in progress banner -->
    <div
      v-if="isMerging"
      class="shrink-0 border-b border-warning/40 bg-warning/10 px-2 py-1.5"
      data-testid="git-merge-in-progress"
    >
      <div class="flex items-center gap-1.5">
        <AlertTriangle :size="13" class="shrink-0 text-warning" />
        <p class="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
          Merge in progress
          <span class="font-normal text-muted-foreground">
            — resolve
            {{ conflictCount === 1 ? '1 conflict' : `${conflictCount} conflicts` }}
            then commit the result
          </span>
        </p>
        <Button
          variant="outline"
          size="sm"
          class="h-6 shrink-0 text-[11px]"
          data-testid="git-merge-abort"
          :disabled="isBusy"
          @click="openAbortDialog"
        >
          Abort merge
        </Button>
      </div>
    </div>

    <!-- Change groups -->
    <ScrollArea class="min-h-0 flex-1">
      <div class="p-1">
        <Collapsible v-model:open="stagedOpen">
          <div class="group/header flex items-center gap-1 rounded-md px-1 py-0.5">
            <CollapsibleTrigger
              class="flex min-w-0 flex-1 items-center gap-1 text-left text-xs font-medium text-foreground"
              data-testid="git-staged-trigger"
            >
              <ChevronDown
                v-if="stagedOpen"
                :size="12"
                class="shrink-0 text-muted-foreground"
              />
              <ChevronDown
                v-else
                :size="12"
                class="shrink-0 -rotate-90 text-muted-foreground"
              />
              Staged Changes
              <span class="text-muted-foreground">
                {{ store.stagedChanges.length }}
              </span>
            </CollapsibleTrigger>
            <Button
              v-if="store.stagedChanges.length > 0"
              variant="ghost"
              size="icon-sm"
              class="h-5 w-5 opacity-0 transition-opacity group-hover/header:opacity-100"
              title="Unstage All Changes"
              data-testid="git-unstage-all"
              @click="void store.unstageAll()"
            >
              <Minus :size="12" />
            </Button>
          </div>
          <CollapsibleContent>
            <div
              v-for="change in store.stagedChanges"
              :key="change.path"
              class="group flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-muted"
              :class="{ 'bg-muted': store.viewingDiffPath === change.path && store.viewingDiffStaged !== false }"
              :data-testid="`git-staged-file`"
              @click="void store.openDiff(change.path, true)"
            >
              <component
                :is="fileIcon(change)"
                :size="13"
                class="shrink-0 text-muted-foreground"
              />
              <span
                class="min-w-0 flex-1 truncate text-xs text-foreground"
                :title="change.path"
                @dblclick.stop="copyPath(change.path)"
              >
                {{ fileName(change.path) }}
                <span class="text-muted-foreground">{{ dirName(change.path) }}</span>
              </span>
              <Button
                variant="ghost"
                size="icon-sm"
                class="h-5 w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                title="Unstage Changes"
                :data-testid="`git-unstage-file`"
                @click.stop="void store.unstage(change.path)"
              >
                <Minus :size="12" />
              </Button>
              <span
                class="w-3 shrink-0 text-center text-[10px] font-semibold"
                :class="STATUS_COLORS[change.status]"
                :title="STATUS_LABELS[change.status]"
              >
                {{ change.status }}
              </span>
            </div>
            <p
              v-if="store.stagedChanges.length === 0"
              class="px-2 py-1 text-[11px] text-muted-foreground"
            >
              No staged changes
            </p>
          </CollapsibleContent>
        </Collapsible>

        <Collapsible v-model:open="changesOpen" class="mt-1">
          <div class="group/header flex items-center gap-1 rounded-md px-1 py-0.5">
            <CollapsibleTrigger
              class="flex min-w-0 flex-1 items-center gap-1 text-left text-xs font-medium text-foreground"
              data-testid="git-changes-trigger"
            >
              <ChevronDown
                v-if="changesOpen"
                :size="12"
                class="shrink-0 text-muted-foreground"
              />
              <ChevronDown
                v-else
                :size="12"
                class="shrink-0 -rotate-90 text-muted-foreground"
              />
              Changes
              <span class="text-muted-foreground">
                {{ store.unstagedChanges.length }}
              </span>
            </CollapsibleTrigger>
            <Button
              v-if="store.unstagedChanges.length > 0"
              variant="ghost"
              size="icon-sm"
              class="h-5 w-5 opacity-0 transition-opacity group-hover/header:opacity-100"
              title="Stage All Changes"
              data-testid="git-stage-all"
              @click="void store.stageAll()"
            >
              <Plus :size="12" />
            </Button>
          </div>
          <CollapsibleContent>
            <div
              v-for="change in store.unstagedChanges"
              :key="change.path"
              class="group flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-muted"
              :class="{ 'bg-muted': store.viewingDiffPath === change.path && store.viewingDiffStaged !== true }"
              data-testid="git-changed-file"
              @click="void store.openDiff(change.path, false)"
            >
              <component
                :is="fileIcon(change)"
                :size="13"
                class="shrink-0 text-muted-foreground"
              />
              <span
                class="min-w-0 flex-1 truncate text-xs text-foreground"
                :title="change.path"
                @dblclick.stop="copyPath(change.path)"
              >
                {{ fileName(change.path) }}
                <span class="text-muted-foreground">{{ dirName(change.path) }}</span>
              </span>
              <Button
                variant="ghost"
                size="icon-sm"
                class="h-5 w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                title="Discard Changes"
                data-testid="git-discard-file"
                @click.stop="discardTarget = change.path"
              >
                <Undo2 :size="12" />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                class="h-5 w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                title="Stage Changes"
                data-testid="git-stage-file"
                @click.stop="void store.stage(change.path)"
              >
                <Plus :size="12" />
              </Button>
              <span
                class="w-3 shrink-0 text-center text-[10px] font-semibold"
                :class="STATUS_COLORS[change.status]"
                :title="STATUS_LABELS[change.status]"
              >
                {{ change.status }}
              </span>
            </div>
            <p
              v-if="store.unstagedChanges.length === 0"
              class="px-2 py-1 text-[11px] text-muted-foreground"
            >
              No changes
            </p>
          </CollapsibleContent>
        </Collapsible>
      </div>
    </ScrollArea>

    <!-- Merge abort confirmation -->
    <Dialog :open="abortDialogOpen" @update:open="(v) => !abortSubmitting && (abortDialogOpen = v)">
      <DialogContent data-testid="git-merge-abort-dialog">
        <DialogHeader>
          <DialogTitle>Abort merge?</DialogTitle>
          <DialogDescription>
            This discards the in-progress merge and restores the working tree
            to its state before the merge. Local merge changes will be lost.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            variant="outline"
            :disabled="abortSubmitting"
            data-testid="git-merge-abort-cancel"
            @click="abortDialogOpen = false"
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            :disabled="abortSubmitting"
            data-testid="git-merge-abort-confirm"
            @click="void confirmAbort()"
          >
            {{ abortSubmitting ? 'Aborting…' : 'Abort merge' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- Discard confirmation -->
    <Dialog
      :open="discardTarget !== null"
      @update:open="(v) => !v && (discardTarget = null)"
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Discard changes</DialogTitle>
          <DialogDescription>
            Are you sure you want to discard changes in
            <span class="font-medium text-foreground">{{ discardTarget }}</span
            >? This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="discardTarget = null">Cancel</Button>
          <Button
            variant="destructive"
            data-testid="git-discard-confirm"
            @click="confirmDiscard"
          >
            Discard
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
