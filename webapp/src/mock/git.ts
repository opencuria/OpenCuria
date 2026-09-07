/**
 * Mock git data for the side-panel Git tab.
 *
 * Frontend-only stand-in until a backend git API exists. `createMockRepos`
 * returns fresh object graphs on every call so Pinia store instances (and
 * tests) never share mutable state.
 */

import type { GitRepo } from '@/types/git'

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * 3_600_000).toISOString()
}

function daysAgo(days: number): string {
  return hoursAgo(days * 24)
}

/** Main workspace repo: feature branches, a merge commit and remote refs. */
function createAppRepo(): GitRepo {
  return {
    id: 'repo-app',
    name: 'opencuria-app',
    path: '/workspace/opencuria',
    currentBranch: 'main',
    headHash: 'f4a9c21',
    branches: [
      { name: 'main', tipHash: 'f4a9c21', ahead: 2, behind: 0 },
      { name: 'feature/git-panel', tipHash: 'g5h1k83', ahead: 0, behind: 0 },
      { name: 'fix/auth-redirect', tipHash: 'a7u2v9w', ahead: 0, behind: 0 },
      { name: 'feature/login-form', tipHash: 'b7c4a19', ahead: 0, behind: 0 },
    ],
    remoteRefs: [
      { name: 'origin/main', tipHash: '9c2f1e7' },
      { name: 'origin/fix/auth-redirect', tipHash: 'a7u2v9w' },
      { name: 'origin/feature/login-form', tipHash: 'b7c4a19' },
    ],
    commits: [
      {
        hash: 'f4a9c21',
        message: 'Fix terminal resize flicker',
        author: 'Timo Kamphaus',
        timestamp: hoursAgo(2),
        parents: ['e8b7d3a'],
      },
      {
        hash: 'g5h1k83',
        message: 'Render commit graph lanes',
        author: 'Timo Kamphaus',
        timestamp: hoursAgo(3),
        parents: ['p2q8r41'],
      },
      {
        hash: 'e8b7d3a',
        message: 'Polish settings sheet spacing',
        author: 'Ada Lovelace',
        timestamp: hoursAgo(5),
        parents: ['9c2f1e7'],
      },
      {
        hash: 'a7u2v9w',
        message: 'Redirect to login on 401',
        author: 'Ada Lovelace',
        timestamp: hoursAgo(6),
        parents: ['e8b7d3a'],
      },
      {
        hash: 'p2q8r41',
        message: 'Scaffold git panel store',
        author: 'Timo Kamphaus',
        timestamp: hoursAgo(8),
        parents: ['3d8e5b2'],
      },
      {
        hash: '9c2f1e7',
        message: "Merge branch 'feature/login-form'",
        author: 'Timo Kamphaus',
        timestamp: daysAgo(1),
        parents: ['3d8e5b2', 'b7c4a19'],
      },
      {
        hash: 'b7c4a19',
        message: 'Style login page',
        author: 'Ada Lovelace',
        timestamp: daysAgo(2),
        parents: ['l1k2j3h'],
      },
      {
        hash: 'l1k2j3h',
        message: 'Add login form validation',
        author: 'Ada Lovelace',
        timestamp: hoursAgo(50),
        parents: ['3d8e5b2'],
      },
      {
        hash: '3d8e5b2',
        message: 'Implement chat composer',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(3),
        parents: ['c2d3e4f'],
      },
      {
        hash: 'c2d3e4f',
        message: 'Add workspace list view',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(5),
        parents: ['c1b2c3d'],
      },
      {
        hash: 'c1b2c3d',
        message: 'Set up Vite + Tailwind',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(8),
        parents: ['c0a1b2c'],
      },
      {
        hash: 'c0a1b2c',
        message: 'Initial commit',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(12),
        parents: [],
      },
    ],
    changes: [
      {
        path: 'webapp/src/components/git/GitPanel.vue',
        status: 'M',
        staged: true,
        diff: [
          {
            header: '@@ -1,4 +1,9 @@',
            oldStart: 1,
            newStart: 1,
            lines: [
              { type: 'context', content: '<script setup lang="ts">' },
              { type: 'del', content: "import { ref } from 'vue'" },
              { type: 'add', content: "import { computed, ref } from 'vue'" },
              { type: 'add', content: "import { useGitStore } from '@/stores/git'" },
              { type: 'add', content: '' },
              { type: 'add', content: 'const store = useGitStore()' },
              { type: 'context', content: '</script>' },
            ],
          },
        ],
      },
      {
        path: 'webapp/src/stores/git.ts',
        status: 'A',
        staged: true,
        diff: [
          {
            header: '@@ -0,0 +1,6 @@',
            oldStart: 0,
            newStart: 1,
            lines: [
              { type: 'add', content: "import { defineStore } from 'pinia'" },
              { type: 'add', content: '' },
              { type: 'add', content: "export const useGitStore = defineStore('git', () => {" },
              { type: 'add', content: '  const repos = ref<GitRepo[]>([])' },
              { type: 'add', content: '  return { repos }' },
              { type: 'add', content: '})' },
            ],
          },
        ],
      },
      {
        path: 'webapp/src/lib/gitGraph.ts',
        status: 'M',
        staged: false,
        diff: [
          {
            header: '@@ -20,7 +20,8 @@',
            oldStart: 20,
            newStart: 20,
            lines: [
              { type: 'context', content: '  const lanes: (string | null)[] = []' },
              { type: 'del', content: '  for (const commit of commits) {' },
              { type: 'add', content: '  for (const commit of ordered) {' },
              { type: 'add', content: '    assignLane(commit, lanes)' },
              { type: 'context', content: '  }' },
              { type: 'context', content: '  return lanes' },
            ],
          },
        ],
      },
      {
        path: 'webapp/src/assets/main.css',
        status: 'M',
        staged: false,
        diff: [
          {
            header: '@@ -118,3 +118,5 @@',
            oldStart: 118,
            newStart: 118,
            lines: [
              { type: 'context', content: '  --color-error: var(--error);' },
              { type: 'add', content: '  --color-graph-edge: var(--chart-1);' },
              { type: 'add', content: '  --color-graph-node: var(--chart-2);' },
              { type: 'context', content: '}' },
            ],
          },
        ],
      },
      {
        path: 'runner/src/legacy_prompt.py',
        status: 'D',
        staged: false,
        diff: [
          {
            header: '@@ -1,4 +0,0 @@',
            oldStart: 1,
            newStart: 0,
            lines: [
              { type: 'del', content: '"""Legacy prompt dispatch (removed)."""' },
              { type: 'del', content: '' },
              { type: 'del', content: 'def dispatch_prompt(...) -> None:' },
              { type: 'del', content: '    ...' },
            ],
          },
        ],
      },
    ],
  }
}

/** Docs repo: linear history with a remote-only commit (behind 1). */
function createDocsRepo(): GitRepo {
  return {
    id: 'repo-docs',
    name: 'docs-site',
    path: '/workspace/docs-site',
    currentBranch: 'main',
    headHash: 'd1o2c3s',
    branches: [
      { name: 'main', tipHash: 'd1o2c3s', ahead: 0, behind: 1 },
      { name: 'feature/i18n', tipHash: 'i1i8n2x', ahead: 0, behind: 0 },
    ],
    remoteRefs: [{ name: 'origin/main', tipHash: 'r3m0t1x' }],
    commits: [
      {
        hash: 'r3m0t1x',
        message: 'Update deployment guide',
        author: 'Ada Lovelace',
        timestamp: hoursAgo(12),
        parents: ['d1o2c3s'],
      },
      {
        hash: 'd1o2c3s',
        message: 'Add German translation',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(1),
        parents: ['d4o5c6s'],
      },
      {
        hash: 'i1i8n2x',
        message: 'Scaffold i18n setup',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(2),
        parents: ['d7o8c9s'],
      },
      {
        hash: 'd4o5c6s',
        message: 'Restructure sidebar docs',
        author: 'Ada Lovelace',
        timestamp: daysAgo(3),
        parents: ['d7o8c9s'],
      },
      {
        hash: 'd7o8c9s',
        message: 'Initial docs import',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(10),
        parents: [],
      },
    ],
    changes: [
      {
        path: 'docs/getting-started.md',
        status: 'M',
        staged: true,
        diff: [
          {
            header: '@@ -8,3 +8,4 @@',
            oldStart: 8,
            newStart: 8,
            lines: [
              { type: 'context', content: '## Quick start' },
              { type: 'del', content: 'Run `docker compose up`.' },
              { type: 'add', content: 'Run `docker compose up -d`.' },
              { type: 'add', content: 'Then open http://localhost:8080.' },
              { type: 'context', content: '' },
            ],
          },
        ],
      },
      {
        path: 'mkdocs.yml',
        status: 'M',
        staged: false,
        diff: [
          {
            header: '@@ -3,4 +3,5 @@',
            oldStart: 3,
            newStart: 3,
            lines: [
              { type: 'context', content: 'nav:' },
              { type: 'context', content: '  - Home: index.md' },
              { type: 'add', content: '  - Deployment: deployment.md' },
              { type: 'context', content: '  - API: api.md' },
            ],
          },
        ],
      },
    ],
  }
}

/** Small dependency repo: two commits, one local change. */
function createApiClientRepo(): GitRepo {
  return {
    id: 'repo-api-client',
    name: 'api-client',
    path: '/workspace/api-client',
    currentBranch: 'main',
    headHash: 'aa11bb2',
    branches: [{ name: 'main', tipHash: 'aa11bb2', ahead: 1, behind: 0 }],
    remoteRefs: [{ name: 'origin/main', tipHash: 'cc33dd4' }],
    commits: [
      {
        hash: 'aa11bb2',
        message: 'Add retry logic',
        author: 'Ada Lovelace',
        timestamp: hoursAgo(4),
        parents: ['cc33dd4'],
      },
      {
        hash: 'cc33dd4',
        message: 'Initial client scaffold',
        author: 'Timo Kamphaus',
        timestamp: daysAgo(6),
        parents: [],
      },
    ],
    changes: [
      {
        path: 'src/client.ts',
        status: 'M',
        staged: false,
        diff: [
          {
            header: '@@ -14,5 +14,6 @@',
            oldStart: 14,
            newStart: 14,
            lines: [
              { type: 'context', content: 'export async function request(path: string) {' },
              { type: 'del', content: '  return fetch(baseUrl + path)' },
              { type: 'add', content: '  return fetchWithRetry(baseUrl + path, { retries: 3 })' },
              { type: 'context', content: '}' },
            ],
          },
        ],
      },
    ],
  }
}

export function createMockRepos(): GitRepo[] {
  return [createAppRepo(), createDocsRepo(), createApiClientRepo()]
}
