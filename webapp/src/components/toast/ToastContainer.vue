<script setup lang="ts">
import { useNotificationStore } from '@/stores/notifications'
import { CheckCircle, XCircle, AlertTriangle, Info, X } from 'lucide-vue-next'
import { computed } from 'vue'

const store = useNotificationStore()

const iconMap = {
  success: CheckCircle,
  error: XCircle,
  warning: AlertTriangle,
  info: Info,
}

const accentMap = {
  success: 'oklch(0.52 0.16 150',
  error:   'oklch(0.55 0.22 27',
  warning: 'oklch(0.65 0.18 80',
  info:    'oklch(0.55 0.20 258',
}

const iconColorMap = {
  success: 'text-success',
  error:   'text-error',
  warning: 'text-warning',
  info:    'text-info',
}

const visibleNotifications = computed(() => store.notifications.slice(-5))
</script>

<template>
  <Teleport to="body">
    <div class="fixed bottom-[var(--sp-3)] right-[var(--sp-3)] z-[100] flex flex-col gap-[var(--sp-2)] w-[360px] max-w-[calc(100vw-2rem)]">
      <TransitionGroup
        enter-active-class="transition-all duration-[350ms]"
        leave-active-class="transition-all duration-[200ms]"
        enter-from-class="opacity-0 translate-y-3 scale-[0.92]"
        enter-to-class="opacity-100 translate-y-0 scale-100"
        leave-from-class="opacity-100 translate-y-0 scale-100"
        leave-to-class="opacity-0 translate-x-4 scale-[0.94]"
        style="[transition-timing-function:var(--spring-bouncy)]"
      >
        <div
          v-for="notification in visibleNotifications"
          :key="notification.id"
          class="flex items-start gap-3 p-4 relative overflow-hidden"
          :style="`
            border-radius: var(--radius-md);
            background: var(--glass-bg-strong);
            backdrop-filter: var(--glass-filter-strong);
            -webkit-backdrop-filter: var(--glass-filter-strong);
            border: 1px solid ${accentMap[notification.type]} / 0.25);
            box-shadow:
              var(--glass-shadow),
              0 0 0 1px ${accentMap[notification.type]} / 0.08) inset;
          `"
        >
          <!-- Accent glow -->
          <div
            aria-hidden="true"
            class="absolute inset-0 pointer-events-none"
            :style="`
              background: radial-gradient(ellipse 60% 50% at 0% 0%, ${accentMap[notification.type]} / 0.10) 0%, transparent 100%);
              border-radius: inherit;
            `"
          />

          <!-- Icon -->
          <component
            :is="iconMap[notification.type]"
            :size="17"
            :class="iconColorMap[notification.type]"
            class="mt-0.5 shrink-0 relative z-10"
          />

          <!-- Text -->
          <div class="flex-1 min-w-0 relative z-10">
            <p
              class="text-sm font-medium text-fg"
              style="letter-spacing: var(--tracking-body)"
            >{{ notification.title }}</p>
            <p
              v-if="notification.message"
              class="text-[12px] text-muted-fg mt-0.5"
              style="letter-spacing: var(--tracking-caption)"
            >{{ notification.message }}</p>
          </div>

          <!-- Dismiss -->
          <button
            class="shrink-0 text-muted-fg hover:text-fg transition-[color,transform] duration-[200ms] cursor-pointer relative z-10 flex items-center justify-center w-5 h-5 rounded-full"
            style="[transition-timing-function:var(--spring-snappy)]"
            @click="store.remove(notification.id)"
          >
            <X :size="13" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>
