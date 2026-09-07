<script setup lang="ts">
/**
 * GitChangesSection — VSCode-style source control section of the Git tab.
 *
 * Commit message box with Commit / Commit & Push split button and a Push
 * button, above collapsible "Staged Changes" and "Changes" groups. File
 * rows offer hover actions (stage/unstage, discard) and open a mock diff
 * in the main area when clicked.
 */
import { computed, ref } from 'vue'
import type { GitFileChange, GitFileStatus } from '@/types/git'
import { useGitStore } from '@/stores/git'
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
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Check,
  ChevronDown,
  FileCode2,
  FileText,
  Minus,
  Plus,
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
  () => message.value.trim().length > 0 && store.stagedChanges.length > 0,
)
const ahead = computed(() => store.currentBranch?.ahead ?? 0)

function doCommit(andPush: boolean): void {
  if (!canCommit.value) return
  if (store.commit(message.value, andPush)) {
    message.value = ''
  }
}

function onMessageKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault()
    doCommit(false)
  }
}

// --- Discard confirmation ---

const discardTarget = ref<string | null>(null)

function confirmDiscard(): void {
  if (discardTarget.value) store.discard(discardTarget.value)
  discardTarget.value = null
}

// --- File row helpers ---

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
}

const STATUS_LABELS: Record<GitFileStatus, string> = {
  M: 'Modified',
  A: 'Added',
  D: 'Deleted',
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
            @click="doCommit(false)"
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
              <DropdownMenuItem @click="doCommit(false)">
                Commit
              </DropdownMenuItem>
              <DropdownMenuItem
                data-testid="git-commit-push"
                @click="doCommit(true)"
              >
                Commit &amp; Push
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <Button
          variant="outline"
          size="sm"
          class="h-7 shrink-0 gap-1.5 text-xs"
          :disabled="ahead === 0"
          title="Push to origin"
          data-testid="git-push"
          @click="store.push()"
        >
          <Upload :size="12" />
          Push
          <span v-if="ahead > 0" class="text-muted-foreground">{{ ahead }}</span>
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
              @click="store.unstageAll()"
            >
              <Minus :size="12" />
            </Button>
          </div>
          <CollapsibleContent>
            <div
              v-for="change in store.stagedChanges"
              :key="change.path"
              class="group flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-muted"
              :class="{ 'bg-muted': store.viewingDiffPath === change.path }"
              :data-testid="`git-staged-file`"
              @click="store.openDiff(change.path)"
            >
              <component
                :is="fileIcon(change)"
                :size="13"
                class="shrink-0 text-muted-foreground"
              />
              <span class="min-w-0 flex-1 truncate text-xs text-foreground">
                {{ fileName(change.path) }}
                <span class="text-muted-foreground">{{ dirName(change.path) }}</span>
              </span>
              <Button
                variant="ghost"
                size="icon-sm"
                class="h-5 w-5 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                title="Unstage Changes"
                :data-testid="`git-unstage-file`"
                @click.stop="store.unstage(change.path)"
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
              @click="store.stageAll()"
            >
              <Plus :size="12" />
            </Button>
          </div>
          <CollapsibleContent>
            <div
              v-for="change in store.unstagedChanges"
              :key="change.path"
              class="group flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-muted"
              :class="{ 'bg-muted': store.viewingDiffPath === change.path }"
              data-testid="git-changed-file"
              @click="store.openDiff(change.path)"
            >
              <component
                :is="fileIcon(change)"
                :size="13"
                class="shrink-0 text-muted-foreground"
              />
              <span class="min-w-0 flex-1 truncate text-xs text-foreground">
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
                @click.stop="store.stage(change.path)"
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
