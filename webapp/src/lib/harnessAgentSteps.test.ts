import { describe, expect, it } from 'vitest'

import type { HarnessPart, HarnessPartType } from '@/types/harness'
import {
  agentPrimaryAction,
  agentStepNumber,
  agentStepStatus,
  buildAgentStepViews,
  hasRunningAgentSequence,
  hasStructuredAgentMeta,
  isFinalMessageError,
  readAgentMeta,
} from './harnessAgentSteps'

let partSeq = 0

function makePart(type: HarnessPartType, overrides: Partial<HarnessPart> = {}): HarnessPart {
  partSeq += 1
  return {
    id: overrides.id ?? `part-${type}-${partSeq}`,
    session_id: 'session-1',
    type,
    state: 'completed',
    title: overrides.title ?? type,
    output: overrides.output ?? '',
    ...overrides,
  }
}

function agentPart(step: number | null, overrides: Partial<HarnessPart> = {}): HarnessPart {
  return makePart('agent', {
    id: `agent-${step ?? 'legacy'}`,
    title: 'Agent plan',
    output: '(Previous action verification)\nok\n(Next Action)\nclick submit',
    ...(step === null ? { meta: {} } : { meta: { step } }),
    ...overrides,
  })
}

const STRUCTURED_META = {
  agent_meta: {
    verification: 'previous ok',
    analysis: 'login form visible',
    next_action: 'click submit',
    action: 'click "Submit"',
    action_kind: 'click',
  },
}

describe('readAgentMeta', () => {
  it('reads nested agent_meta strings and defaults missing fields', () => {
    const part = agentPart(1, { meta: { step: 1, ...STRUCTURED_META } })
    expect(readAgentMeta(part)).toEqual({
      verification: 'previous ok',
      analysis: 'login form visible',
      next_action: 'click submit',
      action: 'click "Submit"',
      action_kind: 'click',
    })
  })

  it('ignores non-object or non-string agent_meta payloads', () => {
    expect(readAgentMeta(agentPart(1, { meta: { step: 1 } }))).toEqual({
      verification: '',
      analysis: '',
      next_action: '',
      action: '',
      action_kind: '',
    })
    expect(readAgentMeta(agentPart(1, { meta: { step: 1, agent_meta: 'nope' } })).action).toBe('')
    expect(
      readAgentMeta(
        agentPart(1, {
          meta: { step: 1, agent_meta: { action: 42, action_kind: ['click'] } },
        }),
      ).action,
    ).toBe('')
  })

  it('detects structured meta and reads step numbers defensively', () => {
    expect(hasStructuredAgentMeta(agentPart(1, { meta: { step: 1, ...STRUCTURED_META } }))).toBe(
      true,
    )
    expect(hasStructuredAgentMeta(agentPart(1))).toBe(false)
    expect(agentStepNumber(agentPart(2))).toBe(2)
    expect(agentStepNumber(agentPart(2, { meta: { step: 'two' } }))).toBe(null)
    expect(agentStepNumber(agentPart(null))).toBe(null)
  })
})

describe('agentPrimaryAction', () => {
  it('prefers the structured action summary as the primary line', () => {
    const part = agentPart(1, { meta: { step: 1, ...STRUCTURED_META } })
    expect(agentPrimaryAction(part)).toEqual({ text: 'Click "Submit"', legacy: false })
  })

  it('formats finish actions without inventing details', () => {
    const done = agentPart(1, {
      meta: { step: 1, agent_meta: { action: 'done', action_kind: 'done' } },
    })
    expect(agentPrimaryAction(done).text).toBe('Finish task')

    const fail = agentPart(1, {
      meta: { step: 1, agent_meta: { action: 'fail', action_kind: 'fail' } },
    })
    expect(agentPrimaryAction(fail).text).toBe('Report failure')
  })

  it('formats a redacted type action as Type', () => {
    const typed = agentPart(1, {
      meta: { step: 1, agent_meta: { action: 'type', action_kind: 'type' } },
    })
    expect(agentPrimaryAction(typed)).toEqual({ text: 'Type', legacy: false })
  })

  it('falls back to next_action and then to a raw first line', () => {
    const nextOnly = agentPart(1, {
      output: 'free-form plan',
      meta: { step: 1, agent_meta: { next_action: 'scroll down\nthen click' } },
    })
    expect(agentPrimaryAction(nextOnly)).toEqual({ text: 'scroll down', legacy: false })

    const legacy = agentPart(1, { output: 'do something\nmore detail', meta: { step: 1 } })
    expect(agentPrimaryAction(legacy)).toEqual({ text: 'do something', legacy: true })
  })
})

describe('agentStepStatus', () => {
  it('is in progress after step-start and completed after step-finish', () => {
    const start = makePart('step-start', { id: 'start-1', meta: { step: 1 } })
    const agent = agentPart(1, { meta: { step: 1, ...STRUCTURED_META } })
    expect(agentStepStatus(agent, [start, agent], 1)).toBe('in_progress')

    const finish = makePart('step-finish', { id: 'finish-1', meta: { step: 1 } })
    expect(agentStepStatus(agent, [start, agent, finish], 1)).toBe('completed')
  })

  it('marks a superseded plan completed and a same-step tool error as error', () => {
    const start1 = makePart('step-start', { id: 'start-1', meta: { step: 1 } })
    const first = agentPart(1, { id: 'agent-1' })
    const start2 = makePart('step-start', { id: 'start-2', meta: { step: 2 } })
    const second = agentPart(2, { id: 'agent-2' })
    expect(agentStepStatus(first, [start1, first, start2, second], 1)).toBe('completed')

    const failedTool = makePart('tool', {
      id: 'tool-err',
      state: 'error',
      meta: { step: 2 },
    })
    expect(agentStepStatus(second, [start1, first, start2, second, failedTool], 2)).toBe('error')
  })

  it('falls back to part state for legacy payloads without a step', () => {
    expect(agentStepStatus(agentPart(null), [agentPart(null)], null)).toBe('completed')
    const running = agentPart(null, { state: 'running' })
    expect(agentStepStatus(running, [running], null)).toBe('in_progress')
  })
})

describe('buildAgentStepViews', () => {
  it('builds one chronological view per agent part with live flags', () => {
    const parts = [
      makePart('step-start', { id: 'start-1', meta: { step: 1 } }),
      agentPart(1, { id: 'agent-1', meta: { step: 1, ...STRUCTURED_META } }),
      makePart('step-finish', { id: 'finish-1', meta: { step: 1 } }),
      makePart('step-start', { id: 'start-2', meta: { step: 2 } }),
      agentPart(2, { id: 'agent-2', meta: { step: 2, ...STRUCTURED_META } }),
    ]

    const views = buildAgentStepViews(parts, { streaming: true })

    expect(views.map((view) => view.step)).toEqual([1, 2])
    expect(views[0]!.status).toBe('completed')
    expect(views[0]!.live).toBe(false)
    expect(views[1]!.status).toBe('in_progress')
    expect(views[1]!.live).toBe(true)
    expect(views.map((view) => view.legacy)).toEqual([false, false])
  })

  it('flags legacy payloads without structured meta', () => {
    const views = buildAgentStepViews([agentPart(3, { id: 'agent-3' })])
    expect(views[0]!.legacy).toBe(true)
    expect(views[0]!.status).toBe('completed')
    expect(views[0]!.live).toBe(false)
  })

  it('marks only the last step as error on a final run-level failure', () => {
    const parts = [
      makePart('step-start', { id: 'start-1', meta: { step: 1 } }),
      agentPart(1, { id: 'agent-1', meta: { step: 1, ...STRUCTURED_META } }),
      makePart('step-finish', { id: 'finish-1', meta: { step: 1 } }),
      makePart('step-start', { id: 'start-2', meta: { step: 2 } }),
      agentPart(2, { id: 'agent-2', meta: { step: 2, ...STRUCTURED_META } }),
      makePart('step-finish', { id: 'finish-2', meta: { step: 2 } }),
    ]

    const failed = buildAgentStepViews(parts, { finish: 'error', messageError: 'boom' })
    expect(failed.map((view) => view.status)).toEqual(['completed', 'error'])

    const aborted = buildAgentStepViews(parts, { finish: 'aborted', messageError: 'x' })
    expect(aborted.map((view) => view.status)).toEqual(['completed', 'error'])

    const interrupted = buildAgentStepViews(parts, { finish: 'interrupted' })
    expect(interrupted.map((view) => view.status)).toEqual(['completed', 'error'])

    const ok = buildAgentStepViews(parts, { finish: 'stop' })
    expect(ok.map((view) => view.status)).toEqual(['completed', 'completed'])
  })

  it('marks a last step without step-finish as error on final failure', () => {
    const parts = [
      makePart('step-start', { id: 'start-1', meta: { step: 1 } }),
      agentPart(1, { id: 'agent-1', meta: { step: 1, ...STRUCTURED_META } }),
      makePart('step-finish', { id: 'finish-1', meta: { step: 1 } }),
      makePart('step-start', { id: 'start-2', meta: { step: 2 } }),
      agentPart(2, { id: 'agent-2', meta: { step: 2, ...STRUCTURED_META } }),
    ]

    const failed = buildAgentStepViews(parts, { finish: 'error', messageError: 'boom' })
    expect(failed.map((view) => view.status)).toEqual(['completed', 'error'])
  })

  it('ignores a stale final error while the turn still streams', () => {
    const parts = [
      makePart('step-start', { id: 'start-9', meta: { step: 9 } }),
      agentPart(9, { id: 'agent-9', meta: { step: 9, ...STRUCTURED_META } }),
      makePart('step-finish', { id: 'finish-9', meta: { step: 9 } }),
    ]

    const views = buildAgentStepViews(parts, {
      streaming: true,
      finish: 'error',
      messageError: 'stale',
    })
    expect(views.map((view) => view.status)).toEqual(['completed'])
    expect(views[0]!.live).toBe(false)
  })
})

describe('hasRunningAgentSequence', () => {
  it('is true while a step is open and false after its finish', () => {
    const open = [
      makePart('step-start', { id: 'start-1', meta: { step: 1 } }),
      agentPart(1, { id: 'agent-1' }),
    ]
    expect(hasRunningAgentSequence(open)).toBe(true)

    const closed = [...open, makePart('step-finish', { id: 'finish-1', meta: { step: 1 } })]
    expect(hasRunningAgentSequence(closed)).toBe(false)
  })

  it('treats step-attributed running reasoning as an active step', () => {
    const reasoning = makePart('reasoning', {
      id: 'r-2',
      state: 'running',
      output: 'thinking',
      meta: { step: 2 },
    })
    expect(hasRunningAgentSequence([reasoning])).toBe(true)
    expect(hasRunningAgentSequence([agentPart(2, { id: 'agent-2' })])).toBe(true)
  })

  it('stays false for plain harness turns without step markers', () => {
    const tool = makePart('tool', { id: 'tool-1', state: 'running' })
    expect(hasRunningAgentSequence([tool])).toBe(false)
    expect(hasRunningAgentSequence([])).toBe(false)
  })
})

describe('isFinalMessageError', () => {
  it('detects error/aborted finishes only once the turn is final', () => {
    expect(isFinalMessageError({ finish: 'error' })).toBe(true)
    expect(isFinalMessageError({ finish: 'aborted' })).toBe(true)
    expect(isFinalMessageError({ finish: 'interrupted' })).toBe(true)
    expect(isFinalMessageError({ messageError: 'boom' })).toBe(true)
    expect(isFinalMessageError({ finish: 'stop' })).toBe(false)
    expect(isFinalMessageError({})).toBe(false)
    expect(isFinalMessageError({ streaming: true, finish: 'error' })).toBe(false)
    expect(isFinalMessageError({ streaming: true, messageError: 'boom' })).toBe(false)
    expect(isFinalMessageError({ streaming: true, finish: 'interrupted' })).toBe(false)
  })
})
