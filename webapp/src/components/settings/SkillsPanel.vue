<!--
  SkillsPanel — skill list plus create/edit/delete dialogs.
-->
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useSkillStore } from '@/stores/skills'
import { useAuthStore } from '@/stores/auth'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import SettingsSection from './SettingsSection.vue'
import SettingsRow from './SettingsRow.vue'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { BookText, Plus, Pencil, Trash2 } from '@lucide/vue'
import type { Skill } from '@/types'

const skillStore = useSkillStore()
const authStore = useAuthStore()

const showCreateDialog = ref(false)
const editingSkill = ref<Skill | null>(null)
const deletingSkill = ref<Skill | null>(null)

const createName = ref('')
const createBody = ref('')
const createIsOrg = ref(false)
const createSubmitting = ref(false)

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
    <SettingsSection
      description="Reusable prompt fragments appended to harness prompts. Personal skills are yours across all organizations; organization skills are shared with all members."
    >
      <template #actions>
        <Button size="sm" @click="openCreate">
          <Plus />
          New Skill
        </Button>
      </template>

      <div
        v-if="skillStore.loading && !skillStore.skills.length"
        class="flex justify-center py-12"
      >
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="skillStore.error"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        {{ skillStore.error }}
      </div>

      <div
        v-else-if="!skillStore.skills.length"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState
          :icon="BookText"
          title="No skills yet"
          description="Create your first skill to inject reusable prompt context into harness sessions."
        />
      </div>

      <div
        v-else
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <SettingsRow v-for="skill in skillStore.skills" :key="skill.id">
          <template #icon>
            <BookText :size="16" />
          </template>
          <div class="min-w-0 space-y-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-medium text-foreground">{{ skill.name }}</span>
              <Badge :variant="skill.scope === 'organization' ? 'default' : 'secondary'">
                {{ skill.scope === 'organization' ? 'Organization' : 'Personal' }}
              </Badge>
            </div>
            <p
              class="text-sm text-muted-foreground line-clamp-2 whitespace-pre-wrap"
              :title="skill.body"
            >
              {{ skill.body }}
            </p>
          </div>
          <template v-if="canEdit(skill)" #actions>
            <Button
              variant="ghost"
              size="icon-sm"
              title="Edit skill"
              @click="openEdit(skill)"
            >
              <Pencil />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              class="text-destructive hover:text-destructive"
              title="Delete skill"
              @click="deletingSkill = skill"
            >
              <Trash2 />
            </Button>
          </template>
        </SettingsRow>
      </div>
    </SettingsSection>

    <Dialog :open="showCreateDialog" @update:open="(v) => !v && (showCreateDialog = false)">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New Skill</DialogTitle>
          <DialogDescription>
            Reusable markdown appended to harness prompts.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
        <form id="create-skill-form" class="flex flex-col gap-4" @submit.prevent="handleCreate">
          <div class="space-y-2">
            <Label for="create-skill-name">Name</Label>
            <Input
              id="create-skill-name"
              v-model="createName"
              placeholder="e.g. TypeScript Expert"
              :disabled="createSubmitting"
            />
          </div>
          <div class="space-y-2">
            <Label for="create-skill-body">Body (Markdown)</Label>
            <Textarea
              id="create-skill-body"
              v-model="createBody"
              :rows="6"
              placeholder="You are an expert TypeScript developer…"
              :disabled="createSubmitting"
            />
          </div>
          <div v-if="authStore.isAdmin" class="flex items-center justify-between gap-3">
            <Label for="create-org-skill" class="cursor-pointer font-normal">
              Share with entire organization
            </Label>
            <Switch id="create-org-skill" v-model="createIsOrg" :disabled="createSubmitting" />
          </div>
        </form>
        </DialogBody>
        <DialogFooter>
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
            form="create-skill-form"
            :disabled="!createName.trim() || !createBody.trim() || createSubmitting"
          >
            {{ createSubmitting ? 'Saving…' : 'Create Skill' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog :open="!!editingSkill" @update:open="(v) => !v && (editingSkill = null)">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit Skill</DialogTitle>
          <DialogDescription>Update the name or body of this skill.</DialogDescription>
        </DialogHeader>
        <DialogBody>
        <form id="edit-skill-form" class="flex flex-col gap-4" @submit.prevent="handleEdit">
          <div class="space-y-2">
            <Label for="edit-skill-name">Name</Label>
            <Input id="edit-skill-name" v-model="editName" :disabled="editSubmitting" />
          </div>
          <div class="space-y-2">
            <Label for="edit-skill-body">Body (Markdown)</Label>
            <Textarea id="edit-skill-body" v-model="editBody" :rows="6" :disabled="editSubmitting" />
          </div>
        </form>
        </DialogBody>
        <DialogFooter>
          <Button
            variant="outline"
            type="button"
            :disabled="editSubmitting"
            @click="editingSkill = null"
          >
            Cancel
          </Button>
          <Button type="submit" form="edit-skill-form" :disabled="editSubmitting">
            {{ editSubmitting ? 'Saving…' : 'Save Changes' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog :open="!!deletingSkill" @update:open="(v) => !v && (deletingSkill = null)">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete Skill</DialogTitle>
          <DialogDescription>
            Delete {{ deletingSkill?.name }}? This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="deletingSkill = null">Cancel</Button>
          <Button variant="destructive" @click="handleDelete">Delete</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
