<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  data: number[]
  color?: string
  strokeWidth?: number
  min?: number
  max?: number
}>(), {
  strokeWidth: 2,
  color: 'currentColor',
  min: 0,
  max: 100
})

const path = computed(() => {
  if (!props.data || props.data.length < 2) return ''

  const effectiveMax = props.max
  const range = effectiveMax - props.min
  
  // We assume the SVG is 100x100 coordinate space for simplicity, 
  // and we let CSS handle the actual size.
  const width = 100
  const height = 100
  
  const stepX = width / (props.data.length - 1)

  return props.data.map((val, i) => {
    const x = i * stepX
    const clampedVal = Math.min(Math.max(val, props.min), effectiveMax)
    const y = height - ((clampedVal - props.min) / (range || 1)) * height
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
})
</script>

<template>
  <svg viewBox="0 0 100 100" preserveAspectRatio="none" class="overflow-visible w-full h-full">
    <path
      :d="path"
      fill="none"
      :stroke="color"
      :stroke-width="strokeWidth"
      stroke-linecap="round"
      stroke-linejoin="round"
      vector-effect="non-scaling-stroke"
    />
  </svg>
</template>
