<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { marked, Renderer } from 'marked'
import DOMPurify from 'dompurify'

// Load all .md files from docs folder recursively
const modules = import.meta.glob('../docs/**/*.md', { as: 'raw', eager: true })

interface DocEntry {
  slug: string
  title: string
  raw: string
  folder: string | null // null = root level
}

interface FolderGroup {
  name: string | null // null = root level docs
  label: string
  docs: DocEntry[]
}

function extractTitle(raw: string): string {
  const match = raw.match(/^#\s+(.+)$/m)
  return match ? match[1]!.trim() : 'Untitled'
}

/** Convert a full import path like "../docs/mcp-connections/playwright-mcp.md"
 *  to a URL-friendly slug like "mcp-connections/playwright-mcp". */
function fileToSlug(path: string): string {
  // Strip the "../docs/" prefix and ".md" suffix
  const relative = path.replace(/^.*\/docs\//, '').replace(/\.md$/, '')
  // Lowercase, keep slashes, replace non-alphanumeric chars (except slashes) with hyphens
  return relative
    .split('/')
    .map((segment) => segment.toLowerCase().replace(/[^a-z0-9]+/g, '-'))
    .join('/')
}

/** Convert a folder name like "mcp-connections" to a human-readable label. */
function folderToLabel(name: string): string {
  return name
    .split('-')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

const docs = computed<DocEntry[]>(() =>
  Object.entries(modules).map(([path, raw]) => {
    const slug = fileToSlug(path)
    const parts = slug.split('/')
    const folder = parts.length > 1 ? parts.slice(0, -1).join('/') : null
    return {
      slug,
      title: extractTitle(raw as string),
      raw: raw as string,
      folder,
    }
  }),
)

/** Docs grouped by folder for sidebar rendering. */
const docGroups = computed<FolderGroup[]>(() => {
  const groupMap = new Map<string | null, DocEntry[]>()
  for (const doc of docs.value) {
    const key = doc.folder
    if (!groupMap.has(key)) groupMap.set(key, [])
    groupMap.get(key)!.push(doc)
  }
  // Root first, then alphabetical folders
  const groups: FolderGroup[] = []
  const rootDocs = groupMap.get(null)
  if (rootDocs) {
    groups.push({ name: null, label: '', docs: rootDocs })
  }
  const folderKeys = [...groupMap.keys()]
    .filter((k): k is string => k !== null)
    .sort()
  for (const key of folderKeys) {
    const topFolder = key.split('/')[0]!
    groups.push({ name: key, label: folderToLabel(topFolder), docs: groupMap.get(key)! })
  }
  return groups
})

const route = useRoute()
const router = useRouter()

const currentSlug = computed(() => {
  const param = route.params.slug
  if (!param) return undefined
  return Array.isArray(param) ? param.join('/') : param
})

// Redirect to first doc if no slug
watch(
  () => [docs.value, currentSlug.value],
  () => {
    if (!currentSlug.value && docs.value.length > 0) {
      router.replace({ name: 'docs-detail', params: { slug: docs.value[0]!.slug.split('/') } })
    }
  },
  { immediate: true },
)

const currentDoc = computed(() =>
  docs.value.find((d) => d.slug === currentSlug.value) ?? null,
)

// Custom renderer that adds id attributes to headings
function buildRenderer(): Renderer {
  const renderer = new Renderer()
  renderer.heading = ({ text, depth }: { text: string; depth: number }) => {
    const id = text
      .toLowerCase()
      .replace(/[^\w\s-]/g, '')
      .trim()
      .replace(/[\s_]+/g, '-')
    return `<h${depth} id="${id}">${text}</h${depth}>`
  }
  return renderer
}

const renderedHtml = computed(() => {
  if (!currentDoc.value) return ''
  const renderer = buildRenderer()
  const raw = marked.parse(currentDoc.value.raw, { renderer }) as string
  return DOMPurify.sanitize(raw)
})

interface TocEntry {
  id: string
  text: string
  depth: number
}

const toc = computed<TocEntry[]>(() => {
  if (!currentDoc.value) return []
  const entries: TocEntry[] = []
  const regex = /^(#{2,3})\s+(.+)$/gm
  let match
  while ((match = regex.exec(currentDoc.value.raw)) !== null) {
    const depth = match[1]!.length
    const text = match[2]!.trim()
    const id = text
      .toLowerCase()
      .replace(/[^\w\s-]/g, '')
      .trim()
      .replace(/[\s_]+/g, '-')
    entries.push({ id, text, depth })
  }
  return entries
})
</script>

<template>
  <!-- Escape AppLayout's padding so the panels stretch edge-to-edge.
       p-6 = 1.5rem each side → total 3rem; lg:p-8 = 2rem each side → total 4rem -->
  <div class="flex -m-6 lg:-m-8 h-[calc(100%+3rem)] lg:h-[calc(100%+4rem)]">
    <!-- Left: file navigation -->
    <aside class="w-52 shrink-0 border-r border-border bg-surface flex flex-col overflow-y-auto">
      <div class="px-4 py-4 border-b border-border">
        <h2 class="text-xs font-semibold text-muted-fg uppercase tracking-wider">Docs</h2>
      </div>
      <nav class="flex flex-col gap-0.5 p-2">
        <template v-for="group in docGroups" :key="group.name ?? '__root__'">
          <!-- Folder label (only for non-root groups) -->
          <div
            v-if="group.name !== null"
            class="mt-3 mb-0.5 px-3 flex items-center gap-1.5 text-xs font-semibold text-muted-fg uppercase tracking-wider"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              class="w-3.5 h-3.5 shrink-0"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z" />
            </svg>
            {{ group.label }}
          </div>
          <!-- Doc links inside the group -->
          <RouterLink
            v-for="doc in group.docs"
            :key="doc.slug"
            :to="{ name: 'docs-detail', params: { slug: doc.slug.split('/') } }"
            class="px-3 py-2 rounded-[var(--radius-md)] text-sm font-medium transition-colors duration-150"
            :class="[
              group.name !== null ? 'pl-6' : '',
              currentSlug === doc.slug
                ? 'bg-primary/10 text-primary'
                : 'text-muted-fg hover:text-fg hover:bg-surface-hover',
            ]"
          >
            {{ doc.title }}
          </RouterLink>
        </template>
      </nav>
    </aside>

    <!-- Middle: rendered markdown -->
    <main class="flex-1 overflow-y-auto px-8 py-8 min-w-0">
      <article
        v-if="renderedHtml"
        class="prose prose-sm dark:prose-invert max-w-3xl"
        v-html="renderedHtml"
      />
      <p v-else class="text-muted-fg text-sm">No document selected.</p>
    </main>

    <!-- Right: TOC (hidden on small screens) -->
    <aside
      v-if="toc.length > 0"
      class="hidden xl:flex w-56 shrink-0 border-l border-border bg-surface flex-col overflow-y-auto"
    >
      <div class="px-4 py-4 border-b border-border">
        <h2 class="text-xs font-semibold text-muted-fg uppercase tracking-wider">On this page</h2>
      </div>
      <nav class="flex flex-col gap-0.5 p-2">
        <a
          v-for="entry in toc"
          :key="entry.id"
          :href="`#${entry.id}`"
          class="text-sm text-muted-fg hover:text-fg transition-colors duration-150 py-0.5 rounded px-2 hover:bg-surface-hover"
          :class="entry.depth === 3 ? 'pl-5' : 'pl-2'"
        >
          {{ entry.text }}
        </a>
      </nav>
    </aside>
  </div>
</template>
