<!--
  SkillsPanel — Extrahierter Skills-Kern aus SkillsView (Schritt 5).

  Enthält Liste + Create/Edit/Delete-Dialoge ohne Page-Header.
  Wird vom Settings-Sheet (Tab "Skills") und weiterhin von
  SkillsView wiederverwendet.
-->
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useSkillStore } from '@/stores/skills'
import { useAuthStore } from '@/stores/auth'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { BookText, Plus, Pencil, Trash2 } from '@lucide/vue'
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
  <div class="space-y-4">
    <div class="flex items-start justify-between gap-3">
      <p class="text-sm text-muted-foreground">
        Reusable prompt fragments appended to harness prompts. Personal skills are yours across all
        organizations; organization skills are shared with all members.
      </p>
      <Button size="sm" class="shrink-0" @click="openCreate">
        <Plus :size="16" class="mr-1.5" />
        New Skill
      </Button>
    </div>

    <!-- Loading -->
    <div
      v-if="skillStore.loading && !skillStore.skills.length"
      class="flex justify-center py-12"
    >
      <LoadingSpinner :size="24" />
    </div>

    <!-- Error -->
    <div
      v-else-if="skillStore.error"
      class="rounded-md border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ skillStore.error }}
    </div>

    <!-- Empty state -->
    <Card v-else-if="!skillStore.skills.length">
      <div class="flex flex-col items-center justify-center py-12 text-center px-6">
        <BookText :size="40" class="text-muted-foreground mb-3" />
        <p class="text-sm font-medium text-foreground">No skills yet</p>
        <p class="text-sm text-muted-foreground mt-1">
          Create your first skill to inject reusable prompt context into harness sessions.
        </p>
      </div>
    </Card>

    <!-- Skill list -->
    <div v-else class="grid gap-3">
      <Card
        v-for="skill in skillStore.skills"
        :key="skill.id"
        class="p-4 flex items-start justify-between gap-4"
      >
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 mb-1.5">
            <span class="font-medium text-foreground text-sm">{{ skill.name }}</span>
            <Badge :variant="skill.scope === 'organization' ? 'default' : 'secondary'">
              {{ skill.scope === 'organization' ? 'Organization' : 'Personal' }}
            </Badge>
          </div>
          <p class="text-xs text-muted-foreground font-mono line-clamp-2 whitespace-pre-wrap">{{
            skill.body
          }}</p>
        </div>
        <div v-if="canEdit(skill)" class="flex items-center gap-1 shrink-0">
          <Button
            variant="ghost"
            size="icon"
            class="h-8 w-8"
            title="Edit skill"
            @click="openEdit(skill)"
          >
            <Pencil :size="14" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            class="h-8 w-8 text-error hover:text-error"
            title="Delete skill"
            @click="deletingSkill = skill"
          >
            <Trash2 :size="14" />
          </Button>
        </div>
      </Card>
    </div>

    <!-- Create dialog -->
    <Dialog :open="showCreateDialog" @update:open="(v) => !v && (showCreateDialog = false)">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New Skill</DialogTitle>
        </DialogHeader>
        <form class="flex flex-col gap-4" @submit.prevent="handleCreate">
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
            <Input
              v-model="createName"
              placeholder="e.g. TypeScript Expert"
              :disabled="createSubmitting"
            />
          </div>
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Body (Markdown)</label>
            <Textarea
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
            <label for="create-org-skill" class="text-sm text-foreground cursor-pointer">
              Share with entire organization
            </label>
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <Button
              variant="outline"
              type="button"
              :disabled="createSubmitting"
              @click="showCreateDialog = false"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              :disabled="!createName.trim() || !createBody.trim() || createSubmitting"
            >
              {{ createSubmitting ? 'Saving…' : 'Create Skill' }}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>

    <!-- Edit dialog -->
    <Dialog :open="!!editingSkill" @update:open="(v) => !v && (editingSkill = null)">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit Skill</DialogTitle>
        </DialogHeader>
        <form class="flex flex-col gap-4" @submit.prevent="handleEdit">
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
            <Input v-model="editName" :disabled="editSubmitting" />
          </div>
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Body (Markdown)</label>
            <Textarea v-model="editBody" :rows="6" :disabled="editSubmitting" />
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <Button
              variant="outline"
              type="button"
              :disabled="editSubmitting"
              @click="editingSkill = null"
            >
              Cancel
            </Button>
            <Button type="submit" :disabled="editSubmitting">
              {{ editSubmitting ? 'Saving…' : 'Save Changes' }}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>

    <!-- Delete confirmation dialog -->
    <Dialog :open="!!deletingSkill" @update:open="(v) => !v && (deletingSkill = null)">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete Skill</DialogTitle>
        </DialogHeader>
        <p class="text-sm text-foreground mb-4">
          Delete <strong>{{ deletingSkill?.name }}</strong
          >? This cannot be undone.
        </p>
        <div class="flex justify-end gap-2">
          <Button variant="outline" @click="deletingSkill = null">Cancel</Button>
          <Button variant="destructive" @click="handleDelete"> Delete </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
