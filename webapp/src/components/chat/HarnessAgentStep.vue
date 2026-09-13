<!--
  HarnessAgentStep — one Agent-S plan step in the computer-use timeline.

  Compact header (timeline node, "Step N" eyebrow, primary action, status)
  with collapsed details (analysis, previous-action verification, full raw
  plan). Reasoning stays a separate chronological Thought row; this card
  only derives status from step-start/step-finish markers. Dark mode and
  responsive layout come from existing theme tokens only.
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, ChevronDown, CircleAlert, MonitorPlay } from '@lucide/vue'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import HarnessMarkdown from './HarnessMarkdown.vue'
import { agentPrimaryAction, readAgentMeta, type AgentStepStatus } from '@/lib/harnessAgentSteps'
import type { HarnessPart } from '@/types/harness'

const props = defineProps<{
  /** Step number (`meta.step`); null for legacy payloads without one. */
  step: number | null
  /** Full `agent` part (plan text in `output`, summaries in meta). */
  part: HarnessPart
  status: AgentStepStatus
  /** True while this is the newest step of a still-running turn. */
  live?: boolean
  /** True when only the raw plan is available (legacy/partial data). */
  legacy?: boolean
  /** True when the next visible block is directly another agent step. */
  connected?: boolean
}>()

const open = ref(false)

const meta = computed(() => readAgentMeta(props.part))
const primary = computed(() => agentPrimaryAction(props.part).text)

const eyebrow = computed(() => (props.step !== null ? `Step ${props.step}` : 'Agent plan'))

const statusLabel = computed(() =>
  props.status === 'completed' ? 'Completed' : props.status === 'error' ? 'Failed' : 'In progress',
)

const statusTestId = 'harness-agent-step-status'

const detailsId = computed(() => `agent-step-details-${props.part.id}`)
const triggerLabel = computed(() =>
  open.value ? 'Hide step details' : `Show step details for ${eyebrow.value}`,
)

const analysis = computed(() => meta.value.analysis)
const verification = computed(() => meta.value.verification)
const nextAction = computed(() => meta.value.next_action)
const rawPlan = computed(() => (props.part.output || '').trim())

const hasDetails = computed(
  () =>
    analysis.value !== '' ||
    verification.value !== '' ||
    nextAction.value !== '' ||
    rawPlan.value !== '',
)

const fullPlanOpen = ref(false)
const fullPlanId = computed(() => `agent-step-plan-${props.part.id}`)
const fullPlanTriggerLabel = computed(() =>
  fullPlanOpen.value ? 'Hide full plan' : 'Show full plan',
)
</script>

<template>
  <section
    data-testid="harness-agent-step"
    :data-part-id="part.id"
    :data-step="step ?? ''"
    :data-status="status"
    :aria-label="`${eyebrow} — ${primary}`"
    class="relative flex gap-2.5 sm:gap-3"
  >
    <!-- Timeline rail: node + connecting line -->
    <div class="flex shrink-0 flex-col items-center" aria-hidden="true">
      <span
        data-testid="harness-agent-step-node"
        class="mt-1 flex size-5 items-center justify-center rounded-full border"
        :class="
          status === 'completed'
            ? 'border-primary/40 bg-primary/10 text-primary'
            : status === 'error'
              ? 'border-destructive/40 bg-destructive/10 text-destructive'
              : 'border-primary/60 bg-primary/15 text-primary'
        "
      >
        <Check v-if="status === 'completed'" :size="12" />
        <CircleAlert v-else-if="status === 'error'" :size="12" />
        <MonitorPlay v-else :size="12" />
      </span>
      <span
        v-if="connected"
        data-testid="harness-agent-step-rail"
        data-connected="true"
        class="w-px flex-1 bg-border"
      />
    </div>

    <div
      class="min-w-0 flex-1 rounded-xl border bg-card px-3 py-2"
      :class="
        status === 'in_progress'
          ? 'border-primary/40'
          : status === 'error'
            ? 'border-destructive/40'
            : 'border-border'
      "
    >
      <div class="flex min-w-0 items-start gap-2">
        <div class="min-w-0 flex-1">
          <p class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            {{ eyebrow }}
          </p>
          <p
            data-testid="harness-agent-step-action"
            class="mt-0.5 line-clamp-2 text-sm font-medium text-foreground"
          >
            {{ primary }}
          </p>
        </div>
        <span
          :data-testid="statusTestId"
          role="status"
          v-bind="live ? { 'aria-live': 'polite' } : {}"
          class="flex shrink-0 items-center gap-1.5 rounded-full border border-border/70 px-2 py-0.5 text-xs"
          :class="
            status === 'completed'
              ? 'text-muted-foreground'
              : status === 'error'
                ? 'text-destructive'
                : 'text-primary'
          "
        >
          <LoadingSpinner
            v-if="status === 'in_progress'"
            :size="12"
            class="motion-reduce:animate-none"
          />
          <Check v-else-if="status === 'completed'" :size="12" aria-hidden="true" />
          <CircleAlert v-else :size="12" aria-hidden="true" />
          {{ statusLabel }}
        </span>
      </div>

      <Collapsible v-if="hasDetails" v-model:open="open" class="mt-1">
        <CollapsibleTrigger
          data-testid="harness-agent-step-details-trigger"
          :aria-label="triggerLabel"
          :aria-controls="detailsId"
          class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ChevronDown
            :size="12"
            class="shrink-0 transition-transform motion-reduce:transition-none"
            :class="open ? 'rotate-180' : ''"
            aria-hidden="true"
          />
          <span>{{ open ? 'Hide details' : 'Show details' }}</span>
        </CollapsibleTrigger>
        <CollapsibleContent :id="detailsId" class="pt-1.5">
          <div class="space-y-2 border-t border-border/60 pt-2">
            <div v-if="analysis" class="space-y-0.5">
              <p class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Analysis
              </p>
              <p class="whitespace-pre-wrap break-words text-xs text-muted-foreground">
                {{ analysis }}
              </p>
            </div>
            <div v-if="verification" class="space-y-0.5">
              <p class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Previous action
              </p>
              <p class="whitespace-pre-wrap break-words text-xs text-muted-foreground">
                {{ verification }}
              </p>
            </div>
            <div v-if="nextAction && nextAction !== primary" class="space-y-0.5">
              <p class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Next action
              </p>
              <p class="whitespace-pre-wrap break-words text-xs text-muted-foreground">
                {{ nextAction }}
              </p>
            </div>
            <Collapsible v-model:open="fullPlanOpen" class="space-y-0.5">
              <CollapsibleTrigger
                data-testid="harness-agent-step-raw-trigger"
                :aria-label="fullPlanTriggerLabel"
                :aria-controls="fullPlanId"
                class="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
              >
                {{ fullPlanOpen ? 'Hide full plan' : 'Show full plan' }}
              </CollapsibleTrigger>
              <CollapsibleContent :id="fullPlanId">
                <div
                  class="max-h-64 overflow-auto rounded-md border border-border/60 bg-muted/30 px-2.5 py-2"
                >
                  <HarnessMarkdown :text="rawPlan" compact />
                </div>
              </CollapsibleContent>
            </Collapsible>
            <p v-if="legacy" class="text-[11px] text-muted-foreground">
              Full plan text — structured details are unavailable for this step.
            </p>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  </section>
</template>
