<script setup lang="ts">
/**
 * One runner of an image definition: build status, the next step as a button,
 * everything else in a menu, and the versions on demand.
 */
import { computed, ref, type Component } from 'vue'
import {
  ChevronDown,
  Eye,
  EyeOff,
  Hammer,
  Loader2,
  MoreHorizontal,
  RefreshCw,
  RotateCcw,
  ScrollText,
  Trash2,
} from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { RunnerStatus, type ImageArtifact, type Runner, type RunnerImageBuild } from '@/types'
import ImageStatusBadge from './ImageStatusBadge.vue'
import ImageVersionList from './ImageVersionList.vue'
import {
  getRunnerBuildActions,
  getRunnerBuildStatus,
  type RunnerBuildAction,
  type RunnerBuildActionId,
} from './imageDefinitionLifecycle'

const props = defineProps<{
  runner: Runner
  build: RunnerImageBuild | null
  /** Admins change builds; members only read status and logs. */
  canManage: boolean
  /** The definition is being deleted. */
  locked: boolean
  busy: boolean
}>()

const emit = defineEmits<{
  action: [action: RunnerBuildAction]
  deleteVersion: [version: ImageArtifact]
}>()

const ACTION_ICONS: Record<RunnerBuildActionId, Component> = {
  build: Hammer,
  rebuild: RefreshCw,
  retry: RotateCcw,
  activate: Eye,
  deactivate: EyeOff,
  remove: Trash2,
  retry_remove: Trash2,
  view_log: ScrollText,
}

const showVersions = ref(false)
const name = computed(() => props.runner.name || props.runner.id.slice(0, 8))
const online = computed(() => props.runner.status === RunnerStatus.ONLINE)
const status = computed(() => getRunnerBuildStatus(props.build, online.value))
const actions = computed(() =>
  getRunnerBuildActions(props.build, { definitionLocked: props.locked }).filter(
    (action) => props.canManage || action.id === 'view_log',
  ),
)
const primary = computed(() => actions.value.find((action) => action.kind === 'primary') ?? null)
const menu = computed(() => actions.value.filter((action) => action !== primary.value))
const versions = computed(() => props.build?.versions ?? [])
</script>

<template>
  <div class="py-2 pl-11 pr-3" data-testid="image-runner-row">
    <div class="flex min-h-8 flex-wrap items-center gap-x-2 gap-y-1">
      <span class="truncate text-sm text-foreground">{{ name }}</span>
      <span v-if="!online" class="text-xs text-muted-foreground">Offline</span>
      <ImageStatusBadge :status="status" />
      <button
        v-if="versions.length"
        type="button"
        class="inline-flex items-center gap-0.5 rounded-sm text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
        :aria-expanded="showVersions"
        data-testid="image-runner-versions-toggle"
        @click="showVersions = !showVersions"
      >
        {{ versions.length }} {{ versions.length === 1 ? 'version' : 'versions' }}
        <ChevronDown
          class="size-3 transition-transform"
          :class="{ 'rotate-180': showVersions }"
          aria-hidden="true"
        />
      </button>

      <div class="ml-auto flex items-center gap-1">
        <Button
          v-if="primary"
          size="sm"
          variant="outline"
          :disabled="busy"
          :data-testid="`image-runner-${primary.id}`"
          @click="emit('action', primary)"
        >
          <Loader2 v-if="busy" class="animate-spin" />
          <component :is="ACTION_ICONS[primary.id]" v-else />
          {{ primary.label }}
        </Button>
        <DropdownMenu v-if="menu.length">
          <DropdownMenuTrigger as-child>
            <Button
              variant="ghost"
              size="icon-sm"
              class="text-muted-foreground"
              :disabled="busy"
              :aria-label="`More actions for ${name}`"
              title="More actions"
              data-testid="image-runner-menu"
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" class="w-44">
            <template v-for="action in menu" :key="action.id">
              <DropdownMenuSeparator v-if="action.kind === 'destructive' && menu.length > 1" />
              <DropdownMenuItem
                :variant="action.kind === 'destructive' ? 'destructive' : 'default'"
                :data-testid="`image-runner-${action.id}`"
                @select="emit('action', action)"
              >
                <component :is="ACTION_ICONS[action.id]" />
                {{ action.label }}
              </DropdownMenuItem>
            </template>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
    <ImageVersionList
      v-if="showVersions && versions.length"
      class="mt-1"
      :versions="versions"
      :can-delete="canManage && !locked"
      @delete="emit('deleteVersion', $event)"
    />
  </div>
</template>
