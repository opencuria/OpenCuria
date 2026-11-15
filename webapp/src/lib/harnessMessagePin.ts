import type { InjectionKey } from 'vue'

export type PinHarnessMessage = (messageId: string, pinned: boolean) => void
export const pinHarnessMessageKey: InjectionKey<PinHarnessMessage> = Symbol('pinHarnessMessage')
