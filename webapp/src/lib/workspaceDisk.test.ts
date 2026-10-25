import { describe, expect, it } from 'vitest'

import { describeDiskUsage, diskUsagePercent, isDiskUsageStale, usageLevel } from './workspaceDisk'

const GIB = 1024 ** 3

function usage(usedGib: number, freeGib: number, totalGib = usedGib + freeGib + 1) {
  return {
    used_bytes: usedGib * GIB,
    free_bytes: freeGib * GIB,
    total_bytes: totalGib * GIB,
    observed_at: '2026-10-25T12:00:00Z',
  }
}

describe('workspaceDisk', () => {
  it('measures usable space like df, so reserved blocks count as unavailable', () => {
    // 19 GiB filesystem with 1 GiB root reserve: 9 used + 9 free → 50 %.
    expect(diskUsagePercent(usage(9, 9, 19))).toBe(50)
    expect(diskUsagePercent(usage(0, 0, 0))).toBe(0)
  })

  it('escalates from ok to warning at 90 % and to critical at 97 %', () => {
    expect(usageLevel(89.9)).toBe('ok')
    expect(usageLevel(90)).toBe('warning')
    expect(usageLevel(96.9)).toBe('warning')
    expect(usageLevel(97)).toBe('critical')
  })

  it('floors the shown percent so a value below the threshold never reads as 90 %', () => {
    const view = describeDiskUsage(usage(8.99, 1.01))
    expect(view.percent).toBe(89)
    expect(view.level).toBe('ok')
    expect(view.capacityBytes).toBe(10 * GIB)
  })

  it('treats observations older than five minutes as stale', () => {
    const observedAt = Date.parse('2026-10-25T12:00:00Z')
    expect(isDiskUsageStale(usage(1, 1), observedAt + 4 * 60_000)).toBe(false)
    expect(isDiskUsageStale(usage(1, 1), observedAt + 6 * 60_000)).toBe(true)
  })
})
