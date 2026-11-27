<!--
  PluginSkillEditor — one skill of a plugin draft: name, the description
  agents decide by, and the Markdown instructions. Pasting a SKILL.md fills
  name and description from its frontmatter.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { BookText, ChevronRight, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { applySkillMarkdown, type PluginSkillForm } from '@opencuria/chat-core/pluginForms'

const props = defineProps<{
  index: number
  errors: string[]
  disabled?: boolean
}>()
const skill = defineModel<PluginSkillForm>({ required: true })
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ remove: [] }>()

const title = computed(() => skill.value.name.trim() || 'New skill')
const field = (name: string) => `skill-${name}-${skill.value.uid}`

function update(patch: Partial<PluginSkillForm>): void {
  skill.value = { ...skill.value, ...patch }
}

function setBody(text: string): void {
  skill.value = applySkillMarkdown(skill.value, text)
}
</script>

<template>
  <Collapsible
    v-model:open="open"
    class="rounded-lg border border-border bg-card"
    :class="errors.length && !open ? 'border-destructive/40' : undefined"
    :data-testid="`plugin-skill-${index}`"
  >
    <div class="flex items-center gap-2 px-3 py-2">
      <CollapsibleTrigger
        class="flex min-w-0 flex-1 items-center gap-2 rounded-md py-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronRight
          :size="15"
          class="shrink-0 text-muted-foreground transition-transform"
          :class="open ? 'rotate-90' : undefined"
        />
        <BookText :size="15" class="shrink-0 text-muted-foreground" />
        <span class="min-w-0 truncate text-sm font-medium">{{ title }}</span>
        <span
          v-if="!open && skill.description"
          class="hidden min-w-0 flex-1 truncate text-xs text-muted-foreground sm:block"
          >{{ skill.description }}</span
        >
        <span v-if="!open && errors.length" class="ml-auto shrink-0 text-xs text-destructive">{{
          errors[0]
        }}</span>
      </CollapsibleTrigger>
      <Button
        size="icon-sm"
        variant="ghost"
        type="button"
        class="text-muted-foreground hover:text-destructive"
        :aria-label="`Remove ${title}`"
        :data-testid="`plugin-remove-skill-${index}`"
        :disabled="props.disabled"
        @click="emit('remove')"
        ><Trash2
      /></Button>
    </div>
    <CollapsibleContent>
      <div class="space-y-4 border-t border-border px-4 py-4">
        <div class="space-y-2">
          <Label :for="field('name')">Name</Label>
          <Input
            :id="field('name')"
            :model-value="skill.name"
            :disabled="disabled"
            @update:model-value="update({ name: String($event) })"
          />
        </div>
        <div class="space-y-2">
          <Label :for="field('description')">When to use it</Label>
          <Textarea
            :id="field('description')"
            :model-value="skill.description"
            :rows="2"
            :disabled="disabled"
            @update:model-value="update({ description: String($event) })"
          />
          <p class="text-xs text-muted-foreground">
            Agents only see this description and load the instructions when a task matches it.
          </p>
        </div>
        <div class="space-y-2">
          <Label :for="field('body')">Instructions</Label>
          <Textarea
            :id="field('body')"
            :model-value="skill.body"
            :rows="8"
            class="font-mono text-xs leading-relaxed"
            :disabled="disabled"
            @update:model-value="setBody(String($event))"
          />
          <p class="text-xs text-muted-foreground">
            Markdown. Paste a SKILL.md to fill in name and description.
          </p>
        </div>
        <ul v-if="errors.length" class="space-y-0.5 text-xs text-destructive">
          <li v-for="error in errors" :key="error">{{ error }}</li>
        </ul>
      </div>
    </CollapsibleContent>
  </Collapsible>
</template>
