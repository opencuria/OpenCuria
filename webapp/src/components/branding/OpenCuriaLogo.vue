<script setup lang="ts">
defineOptions({ inheritAttrs: false })

withDefaults(
  defineProps<{
    iconOnly?: boolean
    alt?: string
  }>(),
  {
    iconOnly: false,
    alt: 'OpenCuria logo',
  },
)
</script>

<template>
  <!--
    Gavel / ⊢ symbol: 3 rectangles
    ① Wide horizontal bar = gavel head (amber)
    ② Narrow connector   = neck (amber, dimmed)
    ③ Narrow vertical    = handle (foreground, muted)
  -->
  <svg
    v-if="iconOnly"
    v-bind="$attrs"
    viewBox="0 0 32 32"
    class="block"
    :class="'w-8 h-8'"
    role="img"
    :aria-label="alt"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
  >
    <!-- ① Head -->
    <rect x="3" y="3" width="26" height="11" rx="3" fill="var(--color-primary)" />
    <!-- ② Neck -->
    <rect x="12" y="12" width="8" height="6" rx="1.5" fill="var(--color-primary)" opacity="0.55" />
    <!-- ③ Handle -->
    <rect x="14" y="17" width="4" height="12" rx="2" fill="currentColor" opacity="0.35" style="color: var(--color-foreground)" />
  </svg>

  <svg
    v-else
    v-bind="$attrs"
    viewBox="0 0 220 32"
    class="block"
    :class="'h-8 w-auto'"
    role="img"
    :aria-label="alt"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
  >
    <!-- Icon: same 3-rect gavel in 32×32 space -->
    <!-- ① Head -->
    <rect x="3" y="3" width="26" height="11" rx="3" fill="var(--color-primary)" />
    <!-- ② Neck -->
    <rect x="12" y="12" width="8" height="6" rx="1.5" fill="var(--color-primary)" opacity="0.55" />
    <!-- ③ Handle -->
    <rect x="14" y="17" width="4" height="12" rx="2" fill="var(--color-foreground)" opacity="0.35" />

    <!-- Wordmark -->
    <text
      x="42"
      y="23"
      font-family="Geist, ui-sans-serif, system-ui, -apple-system, sans-serif"
      font-size="20"
      font-weight="600"
      fill="var(--color-foreground)"
      letter-spacing="-0.4"
    >Open<tspan font-weight="400" fill="var(--color-primary)">Curia</tspan></text>
  </svg>
</template>
