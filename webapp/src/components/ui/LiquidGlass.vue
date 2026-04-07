<script setup lang="ts">
/**
 * LiquidGlass — Apple Liquid Glass base component (2026)
 *
 * Implements the three-layer optical stack:
 * 1. Illumination  — ambient colour bleed from background
 * 2. Glass body    — frosted glass with backdrop-filter
 * 3. Highlight     — specular glint (top-left rim light)
 *
 * Also mounts the SVG refraction filter into <body> once.
 * Uses feDisplacementMap + feTurbulence for subtle lens distortion.
 */

import { onMounted, computed } from 'vue'
import { cn } from '@/lib/utils'

const SVG_FILTER_ID = 'lg-refraction-filter'

/** Mount SVG filter into <body> once */
function mountRefractionFilter(): void {
  if (document.getElementById(SVG_FILTER_ID)) return

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('id', 'lg-svg-filters')
  svg.setAttribute('aria-hidden', 'true')
  svg.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;pointer-events:none;'

  svg.innerHTML = `
    <defs>
      <!-- Liquid Glass Refraction: subtle lens distortion at edges -->
      <filter id="${SVG_FILTER_ID}" x="-5%" y="-5%" width="110%" height="110%"
              color-interpolation-filters="sRGB">
        <!-- Noise texture for dithering (2-3%) -->
        <feTurbulence
          type="fractalNoise"
          baseFrequency="0.0045 0.0040"
          numOctaves="4"
          seed="7"
          stitchTiles="stitch"
          result="noise"
        />
        <!-- Enhance contrast of noise for displacement -->
        <feComponentTransfer in="noise" result="scaled-noise">
          <feFuncR type="linear" slope="0.35" intercept="0.32" />
          <feFuncG type="linear" slope="0.35" intercept="0.32" />
        </feComponentTransfer>
        <!-- Displacement: creates the lens-bending at glass edges -->
        <feDisplacementMap
          in="SourceGraphic"
          in2="scaled-noise"
          scale="3"
          xChannelSelector="R"
          yChannelSelector="G"
          result="displaced"
        />
        <!-- Slight blur to anti-alias displacement artifacts -->
        <feGaussianBlur in="displaced" stdDeviation="0.3" />
      </filter>

      <!-- Subtle noise overlay to prevent colour banding -->
      <filter id="lg-dither-filter">
        <feTurbulence type="fractalNoise" baseFrequency="0.65" numOctaves="3" />
        <feColorMatrix type="saturate" values="0" />
        <feBlend in="SourceGraphic" mode="screen" />
      </filter>
    </defs>
  `

  document.body.appendChild(svg)
}

onMounted(mountRefractionFilter)

// ─── Props ────────────────────────────────────────────────────────────────
const props = withDefaults(
  defineProps<{
    /** Visual intensity of the glass effect */
    variant?: 'default' | 'strong' | 'subtle'
    /** Corner radius variant (squircle) */
    radius?: 'xs' | 'sm' | 'md' | 'lg' | 'xl' | '2xl' | 'full'
    /** Enable SVG refraction filter (performance-heavy, off by default) */
    refraction?: boolean
    /** Extra CSS classes */
    class?: string
    /** Render as a specific HTML element */
    as?: string
    /** Whether to apply active/press scale */
    pressable?: boolean
  }>(),
  {
    variant: 'default',
    radius: 'lg',
    refraction: false,
    as: 'div',
    pressable: false,
  },
)

const glassClass = computed(() =>
  cn(
    // Glass variant
    props.variant === 'strong' ? 'glass glass-strong' :
    props.variant === 'subtle' ? 'glass glass-subtle' :
    'glass',

    // Squircle radius
    `squircle-${props.radius === 'xs' ? 'sm' : props.radius === 'sm' ? 'sm' : props.radius === 'md' ? 'md' : props.radius === 'lg' ? 'lg' : 'xl'}`,

    // Pressable
    props.pressable && 'btn-glass cursor-pointer select-none',

    // Custom classes
    props.class,
  ),
)
</script>

<template>
  <component
    :is="as"
    :class="glassClass"
    :style="refraction ? `filter: url(#${SVG_FILTER_ID})` : undefined"
  >
    <slot />
  </component>
</template>
