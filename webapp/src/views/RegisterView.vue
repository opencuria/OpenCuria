<script setup lang="ts">
import { RouterLink } from 'vue-router'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-background px-4">
    <Card class="w-full max-w-md">
      <CardContent class="pt-6">
        <div class="flex items-center gap-3 mb-6 justify-center">
          <OpenCuriaLogo class="h-10 w-auto" alt="OpenCuria" />
        </div>

        <CardHeader class="px-0 pb-4 text-center">
          <CardTitle>Registration closed</CardTitle>
        </CardHeader>

        <p class="text-sm text-muted-foreground text-center mb-6">
          OpenCuria is currently invite-only. New registrations are not accepted at this time.
        </p>

        <p class="text-center text-sm text-muted-foreground">
          Already have an account?
          <RouterLink to="/login" class="text-primary hover:text-primary/80 font-medium">
            Sign in
          </RouterLink>
        </p>
      </CardContent>
    </Card>
  </div>
</template>
