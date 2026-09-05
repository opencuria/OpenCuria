<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

const router = useRouter()
const authStore = useAuthStore()

const name = ref('')
const error = ref('')

async function handleCreate() {
  error.value = ''

  if (!name.value.trim()) {
    error.value = 'Please provide an organization name.'
    return
  }

  const org = await authStore.createOrganization(name.value.trim())
  if (org) {
    router.push('/')
  } else {
    error.value = 'Failed to create organization. Please try a different name.'
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-background px-4">
    <Card class="w-full max-w-md">
      <CardContent class="pt-6">
        <div class="flex items-center gap-3 mb-6 justify-center">
          <OpenCuriaLogo class="h-10 w-auto" alt="OpenCuria" />
        </div>

        <CardHeader class="px-0 pb-4 text-center">
          <CardTitle>Create your organization</CardTitle>
          <CardDescription>
            You need an organization to start managing runners and workspaces.
          </CardDescription>
        </CardHeader>

        <form @submit.prevent="handleCreate" class="space-y-4">
          <div class="space-y-2">
            <Label for="orgName">Organization name</Label>
            <Input
              id="orgName"
              v-model="name"
              type="text"
              required
              placeholder="My Company"
            />
          </div>

          <p v-if="error" class="text-sm text-destructive">{{ error }}</p>

          <Button type="submit" class="w-full" :disabled="authStore.loading">
            {{ authStore.loading ? 'Creating...' : 'Create organization' }}
          </Button>
        </form>
      </CardContent>
    </Card>
  </div>
</template>
