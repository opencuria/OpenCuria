import { RuntimeType } from '@/types'
import type { Runner } from '@/types'

export function normalizeRuntimeType(runtimeType: string | RuntimeType | null | undefined): string {
  return (runtimeType || '').toString().trim().toLowerCase()
}

export function runnerSupportsRuntime(
  runner: Pick<Runner, 'available_runtimes'> | null | undefined,
  runtimeType: string | RuntimeType | null | undefined,
): boolean {
  const normalizedRuntime = normalizeRuntimeType(runtimeType)
  if (!runner || !normalizedRuntime) return false

  return (runner.available_runtimes || []).some(
    (value) => normalizeRuntimeType(value) === normalizedRuntime,
  )
}

export function filterRunnersByRuntime<T extends Pick<Runner, 'available_runtimes'>>(
  runners: T[],
  runtimeType: string | RuntimeType | null | undefined,
): T[] {
  return runners.filter((runner) => runnerSupportsRuntime(runner, runtimeType))
}
