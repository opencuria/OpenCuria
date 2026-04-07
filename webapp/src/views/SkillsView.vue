<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useSkillStore } from '@/stores/skills'
import { useAuthStore } from '@/stores/auth'
import {
  UiButton,
  UiSpinner,
  UiEmptyState,
  UiCard,
  UiBadge,
  UiDialog,
  UiInput,
  UiTextarea,
} from '@/components/ui'
import { BookText, Plus, Pencil, Trash2 } from 'lucide-vue-next'
import type { Skill } from '@/types'

const skillStore = useSkillStore()
const authStore = useAuthStore()

// Dialog state
const showCreateDialog = ref(false)
const editingSkill = ref<Skill | null>(null)
const deletingSkill = ref<Skill | null>(null)

// Create form
const createName = ref('')
const createBody = ref('')
const createIsOrg = ref(false)
const createSubmitting = ref(false)

// Edit form
const editName = ref('')
const editBody = ref('')
const editSubmitting = ref(false)

onMounted(() => {
  skillStore.fetchSkills()
})

function canEdit(skill: Skill): boolean {
  if (skill.scope === 'personal') return true
  return authStore.isAdmin
}

function openCreate(): void {
  createName.value = ''
  createBody.value = ''
  createIsOrg.value = false
  showCreateDialog.value = true
}

function openEdit(skill: Skill): void {
  editingSkill.value = skill
  editName.value = skill.name
  editBody.value = skill.body
}

async function handleCreate(): Promise<void> {
  if (!createName.value.trim() || !createBody.value.trim()) return
  createSubmitting.value = true
  const ok = await skillStore.createSkill({
    name: createName.value.trim(),
    body: createBody.value.trim(),
    organization_skill: createIsOrg.value,
  })
  createSubmitting.value = false
  if (ok) {
    showCreateDialog.value = false
  }
}

async function handleEdit(): Promise<void> {
  if (!editingSkill.value) return
  editSubmitting.value = true
  const ok = await skillStore.updateSkill(editingSkill.value.id, {
    name: editName.value.trim() || undefined,
    body: editBody.value.trim() || undefined,
  })
  editSubmitting.value = false
  if (ok) {
    editingSkill.value = null
  }
}

async function handleDelete(): Promise<void> {
  if (!deletingSkill.value) return
  await skillStore.deleteSkill(deletingSkill.value.id)
  deletingSkill.value = null
}
</script>

<template>
  <div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-fg">Skills</h2>
        <p class="text-sm text-muted-fg mt-1">
          Reusable prompt fragments appended to agent messages. Personal skills are yours across all
          organizations; organization skills are shared with all members.
        </p>
      </div>
      <UiButton @click="openCreate">
        <Plus :size="16" class="mr-1.5" />
        New Skill
      </UiButton>
    </div>

    <!-- Loading -->
    <div
      v-if="skillStore.loading && !skillStore.skills.length"
      class="flex justify-center py-12"
    >
      <UiSpinner :size="24" />
    </div>

    <!-- Error -->
    <div
      v-else-if="skillStore.error"
      class="rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ skillStore.error }}
    </div>

    <!-- Empty state -->
    <UiEmptyState
      v-else-if="!skillStore.skills.length"
      title="No skills yet"
      description="Create your first skill to inject reusable prompt context into agent sessions."
    >
      <template #icon>
        <BookText :size="40" />
      </template>
    </UiEmptyState>

    <!-- Skill list -->
    <div v-else class="grid gap-3">
      <UiCard
        v-for="skill in skillStore.skills"
        :key="skill.id"
        class="p-4 flex items-start justify-between gap-4"
      >
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 mb-1.5">
            <span class="font-medium text-fg text-sm">{{ skill.name }}</span>
            <UiBadge :variant="skill.scope === 'organization' ? 'info' : 'muted'">
              {{ skill.scope === 'organization' ? 'Organization' : 'Personal' }}
            </UiBadge>
          </div>
          <p class="text-xs text-muted-fg font-mono line-clamp-2 whitespace-pre-wrap">{{
            skill.body
          }}</p>
        </div>
        <div v-if="canEdit(skill)" class="flex items-center gap-1 shrink-0">
          <UiButton
            variant="ghost"
            size="icon"
            class="h-8 w-8"
            title="Edit skill"
            @click="openEdit(skill)"
          >
            <Pencil :size="14" />
          </UiButton>
          <UiButton
            variant="ghost"
            size="icon"
            class="h-8 w-8 text-error hover:text-error"
            title="Delete skill"
            @click="deletingSkill = skill"
          >
            <Trash2 :size="14" />
          </UiButton>
        </div>
      </UiCard>
    </div>

    <!-- Create dialog -->
    <UiDialog
      :open="showCreateDialog"
      title="New Skill"
      @update:open="(v) => !v && (showCreateDialog = false)"
    >
      <template #trigger><span /></template>
      <form class="flex flex-col gap-4" @submit.prevent="handleCreate">
        <div>
          <label class="text-sm font-medium text-fg mb-1.5 block">Name</label>
          <UiInput
            v-model="createName"
            placeholder="e.g. TypeScript Expert"
            :disabled="createSubmitting"
          />
        </div>
        <div>
          <label class="text-sm font-medium text-fg mb-1.5 block">Body (Markdown)</label>
          <UiTextarea
            v-model="createBody"
            :rows="6"
            placeholder="You are an expert TypeScript developer…"
            :disabled="createSubmitting"
          />
        </div>
        <div v-if="authStore.isAdmin" class="flex items-center gap-2">
          <input
            id="create-org-skill"
            v-model="createIsOrg"
            type="checkbox"
            class="rounded border-border"
          />
          <label for="create-org-skill" class="text-sm text-fg cursor-pointer">
            Share with entire organization
          </label>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <UiButton
            variant="outline"
            type="button"
            :disabled="createSubmitting"
            @click="showCreateDialog = false"
          >
            Cancel
          </UiButton>
          <UiButton
            type="submit"
            :disabled="!createName.trim() || !createBody.trim() || createSubmitting"
          >
            {{ createSubmitting ? 'Saving…' : 'Create Skill' }}
          </UiButton>
        </div>
      </form>
    </UiDialog>

    <!-- Edit dialog -->
    <UiDialog
      :open="!!editingSkill"
      title="Edit Skill"
      @update:open="(v) => !v && (editingSkill = null)"
    >
      <template #trigger><span /></template>
      <form class="flex flex-col gap-4" @submit.prevent="handleEdit">
        <div>
          <label class="text-sm font-medium text-fg mb-1.5 block">Name</label>
          <UiInput v-model="editName" :disabled="editSubmitting" />
        </div>
        <div>
          <label class="text-sm font-medium text-fg mb-1.5 block">Body (Markdown)</label>
          <UiTextarea v-model="editBody" :rows="6" :disabled="editSubmitting" />
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <UiButton
            variant="outline"
            type="button"
            :disabled="editSubmitting"
            @click="editingSkill = null"
          >
            Cancel
          </UiButton>
          <UiButton type="submit" :disabled="editSubmitting">
            {{ editSubmitting ? 'Saving…' : 'Save Changes' }}
          </UiButton>
        </div>
      </form>
    </UiDialog>

    <!-- Delete confirmation dialog -->
    <UiDialog
      :open="!!deletingSkill"
      title="Delete Skill"
      @update:open="(v) => !v && (deletingSkill = null)"
    >
      <template #trigger><span /></template>
      <p class="text-sm text-fg mb-4">
        Delete <strong>{{ deletingSkill?.name }}</strong>? This cannot be undone. Sessions that
        previously used this skill will keep their snapshot.
      </p>
      <div class="flex justify-end gap-2">
        <UiButton variant="outline" @click="deletingSkill = null">Cancel</UiButton>
        <UiButton
          class="bg-error text-error-fg hover:bg-error/90"
          @click="handleDelete"
        >
          Delete
        </UiButton>
      </div>
    </UiDialog>
  </div>
</template>
