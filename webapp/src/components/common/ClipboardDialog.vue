<script setup lang="ts">
import { onBeforeUnmount } from 'vue'
import { useClipboardStore } from '@/stores/clipboard'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

const clipboard = useClipboardStore()
onBeforeUnmount(() => clipboard.finish())
function selectText(event: FocusEvent): void {
  const textarea = event.target
  if (textarea instanceof HTMLTextAreaElement) textarea.select()
}
</script>

<template>
  <Dialog
    :open="!!clipboard.request"
    @update:open="
      (open) => {
        if (!open) clipboard.finish()
      }
    "
  >
    <DialogContent
      v-if="clipboard.request"
      data-clipboard-dialog
      class="z-[1000]"
      overlay-class="z-[1000]"
    >
      <DialogHeader>
        <DialogTitle>Copy {{ clipboard.request.label }}</DialogTitle>
        <DialogDescription>
          Use Copy now, or select the text and press ⌘C or Ctrl+C.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        <p v-if="clipboard.request.loading" role="status" class="text-muted-foreground">
          Loading clipboard content…
        </p>
        <Textarea
          v-else
          :model-value="clipboard.request.text"
          readonly
          aria-label="Text to copy"
          class="max-h-80 min-h-24 font-mono text-xs"
          @focus="selectText"
        />
        <p v-if="clipboard.request.error" role="alert" class="mt-2 text-destructive">
          {{ clipboard.request.error }}
        </p>
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" @click="clipboard.finish()">Close</Button>
        <Button
          :disabled="clipboard.request.loading || clipboard.request.loadFailed || clipboard.busy"
          @click="clipboard.copyNow"
          >Copy now</Button
        >
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
