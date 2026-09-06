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
  it('orders sheets by priority: mention > question > permission > notice > processes > context > todos', () => {
    const sheets = buildComposerSheets({
      todos: [makeTodo('t1')],
      permissions: [makePermission('p1')],
      questions: [makeQuestion('q1')],
      notice: { messageId: 'msg-1', text: 'Run stopped by user', tone: 'info' },
      mention: {
        candidates: [{ kind: 'file', label: 'a.ts', insert: 'file:a.ts' }],
        activeIndex: 0,
      },
      processesOpen: true,
      contextOpen: true,
      context: { used: 1000, limit: 10_000, percent: 10 },
    })

    expect(sheets.map((sheet) => sheet.kind)).toEqual([
      'mention',
      'question',
      'permission',
      'notice',
      'processes',
      'context',
      'todos',
    ])
  })

  it('omits the context sheet when it is closed', () => {
    const sheets = buildComposerSheets({
      contextOpen: false,
      context: { used: 1000, limit: 10_000, percent: 10 },
      todos: [makeTodo('t1')],
    })

    expect(sheets.map((sheet) => sheet.kind)).toEqual(['todos'])
  })

  it('omits the processes sheet when it is closed', () => {
    const sheets = buildComposerSheets({
      processesOpen: false,
      todos: [makeTodo('t1')],
    })

    expect(sheets.map((sheet) => sheet.kind)).toEqual(['todos'])
  })

  it('orders sheets by priority: mention > question > permission > todos when context is closed', () => {
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

  it('includes a notice sheet when a run error is present', () => {
    const sheets = buildComposerSheets({
      notice: { messageId: 'msg-1', text: 'boom', tone: 'error' },
      todos: [makeTodo('t1')],
    })

    expect(sheets.map((sheet) => sheet.kind)).toEqual(['notice', 'todos'])
    expect(sheets[0]?.notice?.text).toBe('boom')
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

  it('keeps multiple pending permissions on one sheet for the pager', () => {
    const sheets = buildComposerSheets({
      permissions: [makePermission('p1'), makePermission('p2')],
    })

    expect(sheets).toHaveLength(1)
    expect(sheets[0]?.kind).toBe('permission')
    expect(sheets[0]?.permissions).toHaveLength(2)
    expect(sheets[0]?.permission?.request_id).toBe('p1')
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
