import type { InjectionKey, Ref } from 'vue'

/** Workspace id for harness chat descendants (media fetch, etc.). */
export const harnessWorkspaceIdKey: InjectionKey<Ref<string>> = Symbol('harnessWorkspaceId')
