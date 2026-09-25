import { describe, expect, it } from 'vitest'

import {
  basenameOf,
  DEFAULT_FILE_ICON,
  DEFAULT_FOLDER_ICON,
  DEFAULT_FOLDER_OPEN_ICON,
  resolveFileIconKey,
  resolveFolderIconKey,
  resolveWorkspaceIconKey,
} from './fileIcons'

describe('fileIcons resolver', () => {
  it('resolves common source extensions to colored keys', () => {
    expect(resolveFileIconKey({ path: '/workspace/src/main.ts' })).toBe('typescript')
    expect(resolveFileIconKey({ path: '/workspace/src/app.js' })).toBe('javascript')
    expect(resolveFileIconKey({ path: '/workspace/src/App.vue' })).toBe('vue')
    expect(resolveFileIconKey({ name: 'Component.tsx' })).toBe('react_ts')
    expect(resolveFileIconKey({ name: 'widget.jsx' })).toBe('react')
    expect(resolveFileIconKey({ path: '/workspace/server.py' })).toBe('python')
    expect(resolveFileIconKey({ path: '/workspace/main.go' })).toBe('go')
    expect(resolveFileIconKey({ path: '/workspace/main.rs' })).toBe('rust')
    expect(resolveFileIconKey({ name: 'Main.java' })).toBe('java')
    expect(resolveFileIconKey({ name: 'style.scss' })).toBe('sass')
  })

  it('prefers exact file names over extensions', () => {
    expect(resolveFileIconKey({ path: '/workspace/package.json' })).toBe('nodejs')
    expect(resolveFileIconKey({ name: 'Dockerfile' })).toBe('docker')
    expect(resolveFileIconKey({ name: 'docker-compose.yml' })).toBe('docker')
    expect(resolveFileIconKey({ name: 'Makefile' })).toBe('makefile')
    expect(resolveFileIconKey({ name: 'README.md' })).toBe('readme')
    expect(resolveFileIconKey({ name: 'LICENSE' })).toBe('license')
    expect(resolveFileIconKey({ name: 'vite.config.ts' })).toBe('vite')
    expect(resolveFileIconKey({ name: 'vitest.config.ts' })).toBe('vitest')
    expect(resolveFileIconKey({ name: 'tsconfig.app.json' })).toBe('tsconfig')
    expect(resolveFileIconKey({ name: 'go.mod' })).toBe('go-mod')
    expect(resolveFileIconKey({ name: 'requirements.txt' })).toBe('python-misc')
    expect(resolveFileIconKey({ name: 'pyproject.toml' })).toBe('python-misc')
    expect(resolveFileIconKey({ name: '.gitignore' })).toBe('git')
    expect(resolveFileIconKey({ name: '.nvmrc' })).toBe('nodejs')
    expect(resolveFileIconKey({ name: '.env.local' })).toBe('tune')
    expect(resolveFileIconKey({ name: 'eslint.config.js' })).toBe('eslint')
    expect(resolveFileIconKey({ name: 'yarn.lock' })).toBe('yarn')
    expect(resolveFileIconKey({ name: 'CHANGELOG.md' })).toBe('changelog')
  })

  it('maps media, archives, and config extensions broadly', () => {
    expect(resolveFileIconKey({ name: 'logo.png' })).toBe('image')
    expect(resolveFileIconKey({ name: 'photo.jpg' })).toBe('image')
    expect(resolveFileIconKey({ name: 'doc.pdf' })).toBe('pdf')
    expect(resolveFileIconKey({ name: 'archive.zip' })).toBe('zip')
    expect(resolveFileIconKey({ name: 'bundle.tar.gz' })).toBe('zip')
    expect(resolveFileIconKey({ name: 'run.sh' })).toBe('console')
    expect(resolveFileIconKey({ name: 'query.sql' })).toBe('database')
    expect(resolveFileIconKey({ name: 'error.log' })).toBe('log')
    expect(resolveFileIconKey({ name: 'notes.txt' })).toBe('document')
    expect(resolveFileIconKey({ name: 'data.csv' })).toBe('table')
    expect(resolveFileIconKey({ name: 'icon.svg' })).toBe('svg')
    expect(resolveFileIconKey({ name: 'settings.toml' })).toBe('toml')
    expect(resolveFileIconKey({ name: 'data.yaml' })).toBe('yaml')
    expect(resolveFileIconKey({ name: 'config.yml' })).toBe('yaml')
    expect(resolveFileIconKey({ name: 'schema.graphql' })).toBe('graphql')
  })

  it('is case-insensitive and falls back to the generic file icon', () => {
    expect(resolveFileIconKey({ name: 'APP.VUE' })).toBe('vue')
    expect(resolveFileIconKey({ name: 'README.MD' })).toBe('readme')
    expect(resolveFileIconKey({ name: 'mystery.unknownext' })).toBe(DEFAULT_FILE_ICON)
    expect(resolveFileIconKey({ name: 'noextension' })).toBe(DEFAULT_FILE_ICON)
    expect(resolveFileIconKey({})).toBe(DEFAULT_FILE_ICON)
  })

  it('resolves meaningful folder names with open variants', () => {
    expect(resolveFolderIconKey({ name: 'src', directory: true })).toBe('folder-src')
    expect(resolveFolderIconKey({ name: 'src', directory: true, expanded: true })).toBe(
      'folder-src-open',
    )
    expect(resolveFolderIconKey({ path: '/workspace/node_modules', directory: true })).toBe(
      'folder-node',
    )
    expect(
      resolveFolderIconKey({ path: '/workspace/.git', directory: true, expanded: true }),
    ).toBe('folder-git-open')
    expect(resolveFolderIconKey({ name: 'docs', directory: true })).toBe('folder-docs')
    expect(resolveFolderIconKey({ name: 'mystery-dir', directory: true })).toBe(
      DEFAULT_FOLDER_ICON,
    )
    expect(
      resolveFolderIconKey({ name: 'mystery-dir', directory: true, expanded: true }),
    ).toBe(DEFAULT_FOLDER_OPEN_ICON)
  })

  it('routes through the unified entry point', () => {
    expect(resolveWorkspaceIconKey({ path: '/workspace/a.ts' })).toBe('typescript')
    expect(resolveWorkspaceIconKey({ path: '/workspace/src', directory: true })).toBe(
      'folder-src',
    )
    expect(basenameOf('/workspace/a/b/')).toBe('b')
    expect(basenameOf('/workspace/a.ts')).toBe('a.ts')
  })
})
