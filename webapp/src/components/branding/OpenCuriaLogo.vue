<script setup lang="ts">
/**
 * OpenCuria mark (isometric hex) plus optional wordmark.
 *
 * `motion` is opt-in and never applied to chrome (sidebar/header):
 * - idle: slow breath on the home hero
 * - working: facet chase for in-flight work (use at ≥16px)
 * - enter: one-shot assemble on auth screens
 */
import { computed, useAttrs } from 'vue'
import type { ClassValue } from 'clsx'
import { cn } from '@/lib/utils'

export type OpenCuriaLogoMotion = 'none' | 'idle' | 'working' | 'enter'

defineOptions({ inheritAttrs: false })

const props = withDefaults(
  defineProps<{
    iconOnly?: boolean
    alt?: string
    motion?: OpenCuriaLogoMotion
  }>(),
  {
    iconOnly: false,
    alt: 'OpenCuria logo',
    motion: 'none',
  },
)

const attrs = useAttrs()

const decorative = computed(() => !props.alt)

const svgAttrs = computed(() => {
  const rest = { ...attrs }
  delete rest.class
  return rest
})

const rootClass = computed(() =>
  cn(
    'oc-logo overflow-visible',
    props.iconOnly ? 'block size-8' : 'block h-10 w-auto',
    props.motion !== 'none' ? `oc-logo--${props.motion}` : null,
    attrs.class as ClassValue,
  ),
)
</script>

<template>
  <svg
    v-bind="svgAttrs"
    :viewBox="iconOnly ? '13 13 38 38' : '0 0 244 64'"
    :class="rootClass"
    style="color: var(--color-foreground)"
    :role="decorative ? undefined : 'img'"
    :aria-label="decorative ? undefined : alt"
    :aria-hidden="decorative ? 'true' : undefined"
    :data-motion="motion"
  >
    <g class="oc-mark">
      <g class="oc-face oc-face--tl">
        <polygon points="18,23 32,15 32,32" fill="currentColor" opacity="0.24" />
      </g>
      <g class="oc-face oc-face--tr">
        <polygon points="32,15 46,23 32,32" fill="currentColor" opacity="0.42" />
      </g>
      <g class="oc-face oc-face--r oc-face--primary">
        <polygon points="46,23 32,32 46,41" fill="var(--color-primary)" />
      </g>
      <g class="oc-face oc-face--br oc-face--primary">
        <polygon points="32,32 32,49 46,41" fill="var(--color-primary-hover)" />
      </g>
      <g class="oc-face oc-face--l">
        <polygon points="18,23 32,32 18,41" fill="currentColor" opacity="0.18" />
      </g>
      <g class="oc-face oc-face--bl">
        <polygon points="32,32 32,49 18,41" fill="currentColor" opacity="0.1" />
      </g>
      <g class="oc-edges">
        <polygon
          points="18,23 32,15 46,23 46,41 32,49 18,41"
          fill="none"
          stroke="currentColor"
          stroke-opacity="0.22"
          stroke-width="1.2"
        />
        <line x1="18" y1="23" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
        <line x1="32" y1="15" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
        <line x1="46" y1="23" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
        <line x1="32" y1="32" x2="46" y2="41" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
        <line x1="32" y1="32" x2="32" y2="49" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
        <line x1="32" y1="32" x2="18" y2="41" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
      </g>
    </g>
    <text
      v-if="!iconOnly"
      class="oc-wordmark"
      x="78"
      y="42"
      font-family="SF Pro Display, Inter, Segoe UI, sans-serif"
      font-size="30"
      font-weight="600"
      fill="var(--color-foreground)"
      letter-spacing="-0.6"
    >
      Open<tspan font-weight="400" fill="var(--color-primary)">Curia</tspan>
    </text>
  </svg>
</template>

<style scoped>
.oc-mark {
  transform-box: fill-box;
  transform-origin: center;
}

.oc-face--tl polygon {
  --oc-rest: 0.24;
  --oc-peak: 0.5;
}

.oc-face--tr polygon {
  --oc-rest: 0.42;
  --oc-peak: 0.68;
}

.oc-face--l polygon {
  --oc-rest: 0.18;
  --oc-peak: 0.44;
}

.oc-face--bl polygon {
  --oc-rest: 0.1;
  --oc-peak: 0.36;
}

@media (prefers-reduced-motion: no-preference) {
  .oc-logo--idle .oc-mark {
    animation: oc-idle-scale 8s ease-in-out infinite;
  }

  .oc-logo--idle .oc-face--primary {
    animation: oc-idle-primary 8s ease-in-out infinite;
  }

  .oc-logo--working .oc-face--tl polygon,
  .oc-logo--working .oc-face--tr polygon,
  .oc-logo--working .oc-face--l polygon,
  .oc-logo--working .oc-face--bl polygon {
    animation: oc-working-muted 2.4s ease-in-out infinite;
  }

  .oc-logo--working .oc-face--primary polygon {
    animation: oc-working-primary 2.4s ease-in-out infinite;
  }

  .oc-logo--working .oc-face--tl polygon {
    animation-delay: 0s;
  }

  .oc-logo--working .oc-face--tr polygon {
    animation-delay: 0.4s;
  }

  .oc-logo--working .oc-face--r polygon {
    animation-delay: 0.8s;
  }

  .oc-logo--working .oc-face--br polygon {
    animation-delay: 1.2s;
  }

  .oc-logo--working .oc-face--bl polygon {
    animation-delay: 1.6s;
  }

  .oc-logo--working .oc-face--l polygon {
    animation-delay: 2s;
  }

  .oc-logo--enter .oc-face,
  .oc-logo--enter .oc-edges,
  .oc-logo--enter .oc-wordmark {
    opacity: 0;
    animation: oc-enter 320ms ease-out forwards;
  }

  .oc-logo--enter .oc-face--bl {
    animation-delay: 0ms;
  }

  .oc-logo--enter .oc-face--l {
    animation-delay: 40ms;
  }

  .oc-logo--enter .oc-face--tl {
    animation-delay: 80ms;
  }

  .oc-logo--enter .oc-face--tr {
    animation-delay: 120ms;
  }

  .oc-logo--enter .oc-edges {
    animation-delay: 140ms;
  }

  .oc-logo--enter .oc-face--br {
    animation-delay: 180ms;
  }

  .oc-logo--enter .oc-face--r {
    animation-delay: 220ms;
  }

  .oc-logo--enter .oc-wordmark {
    animation-delay: 260ms;
  }
}

@keyframes oc-idle-scale {
  0%,
  100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.025);
  }
}

@keyframes oc-idle-primary {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.88;
  }
}

@keyframes oc-working-muted {
  0%,
  100% {
    opacity: var(--oc-rest);
  }
  14% {
    opacity: var(--oc-peak);
  }
  32% {
    opacity: var(--oc-rest);
  }
}

@keyframes oc-working-primary {
  0%,
  100% {
    filter: brightness(1);
  }
  14% {
    filter: brightness(1.16);
  }
  32% {
    filter: brightness(1);
  }
}

@keyframes oc-enter {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}
</style>
