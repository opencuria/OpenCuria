import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ImageDefinitionModal from './ImageDefinitionModal.vue'
import type { ImageDefinition } from '@/types'

const slot = { template: '<div><slot /></div>' }
const stubs = {
  Dialog: { props: ['open'], template: '<div v-if="open"><slot /></div>' },
  DialogContent: slot,
  DialogHeader: slot,
  DialogTitle: slot,
  DialogDescription: slot,
  DialogBody: slot,
  DialogFooter: slot,
  Select: slot,
  SelectTrigger: slot,
  SelectValue: slot,
  SelectContent: slot,
  SelectItem: { props: ['value'], template: '<div role="option"><slot /></div>' },
}

function definition(overrides: Partial<ImageDefinition> = {}): ImageDefinition {
  return {
    id: 'def-1',
    organization_id: 'org-1',
    name: 'Recipe',
    description: '',
    is_standard: false,
    base_distro: 'ubuntu:24.04',
    packages: [],
    env_vars: {},
    custom_init_script: '',
    is_active: true,
    status: 'active',
    created_at: '2026-09-05T00:00:00Z',
    updated_at: '2026-09-05T00:00:00Z',
    ...overrides,
  }
}

async function openModal(imageDefinition: ImageDefinition) {
  const wrapper = mount(ImageDefinitionModal, {
    props: { open: true, imageDefinition },
    global: { stubs },
  })
  await flushPromises()
  return wrapper
}

describe('ImageDefinitionModal', () => {
  it('offers Ubuntu and Windows base images', async () => {
    const wrapper = await openModal(definition())

    expect(wrapper.findAll('[role="option"]').map((option) => option.text())).toEqual([
      'Ubuntu 22.04',
      'Ubuntu 24.04',
      'Windows 11 Pro',
      'Windows 11 Enterprise',
      'Windows 11 Education',
      'Windows 11 Pro Education',
      'Windows 11 Pro for Workstations',
      'Other…',
    ])
  })

  it('uses winget and PowerShell placeholders for Windows recipes', async () => {
    const wrapper = await openModal(
      definition({ base_distro: 'windows:11', custom_init_script: "Write-Host 'hi'" }),
    )

    expect(wrapper.get('#image-definition-packages').attributes('placeholder')).toBe(
      'Git.Git, Python.Python.3.12',
    )
    expect(wrapper.get('#image-definition-script').attributes('placeholder')).toBe('# PowerShell')
  })

  it('uses apt and bash placeholders for Ubuntu recipes', async () => {
    const wrapper = await openModal(definition({ custom_init_script: 'echo hi' }))

    expect(wrapper.get('#image-definition-packages').attributes('placeholder')).toBe(
      'python3, nodejs, ffmpeg',
    )
    expect(wrapper.get('#image-definition-script').attributes('placeholder')).toBe('#!/bin/bash')
  })
})
