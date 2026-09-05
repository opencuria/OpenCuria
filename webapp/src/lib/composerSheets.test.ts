import { describe, expect, it } from 'vitest'

import { buildComposerSheets, optionLetter } from './composerSheets'
import type { HarnessPermissionRequest, HarnessQuestionRequest, HarnessTodo } from '@/types/harness'

function makeQuestion(id: string): HarnessQuestionRequest {
  return {
    request_id: id,
    session_id: 'session-1',
    workspace_id: 'ws-1',
    questions: [{ question: `Question ${id}` }],
  }
}

function makePermission(id: string): HarnessPermissionRequest {
  return {
    request_id: id,
    session_id: 'session-1',
    workspace_id: 'ws-1',
    tool: 'bash',
    pattern: 'ls',
    title: 'Run ls',
  }
}

function makeTodo(id: string): HarnessTodo {
  return { id, content: id, status: 'pending', priority: 'medium', order: 0 }
}

describe('buildComposerSheets', () => {
  it('orders sheets by priority: mention > question > permission > todos', () => {
    const sheets = buildComposerSheets({
      todos: [makeTodo('t1')],
      permissions: [makePermission('p1')],
      questions: [makeQuestion('q1')],
      mention: {
        candidates: [{ kind: 'file', label: 'a.ts', insert: 'file:a.ts' }],
        activeIndex: 0,
      },
    })

    expect(sheets.map((sheet) => sheet.kind)).toEqual([
      'mention',
      'question',
      'permission',
      'todos',
    ])
  })

  it('returns an empty stack when nothing is active', () => {
    expect(
      buildComposerSheets({ mention: null, questions: [], permissions: [], todos: [] }),
    ).toEqual([])
  })

  it('skips empty sources instead of rendering placeholder sheets', () => {
    const sheets = buildComposerSheets({
      mention: { candidates: [], activeIndex: 0 },
      questions: [],
      permissions: [],
      todos: [makeTodo('t1')],
    })

    expect(sheets.map((sheet) => sheet.kind)).toEqual(['todos'])
  })
})

describe('optionLetter', () => {
  it('labels the first options A through E', () => {
    expect([0, 1, 2, 3, 4].map(optionLetter)).toEqual(['A', 'B', 'C', 'D', 'E'])
  })

  it('continues past Z with double letters', () => {
    expect(optionLetter(25)).toBe('Z')
    expect(optionLetter(26)).toBe('AA')
  })
})
