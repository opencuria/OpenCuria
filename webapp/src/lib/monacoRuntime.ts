// Keep the standalone editor and its language services, without the separate
// LSP client exported by Monaco's package-wide entry point.
import 'monaco-editor/features/register.all.js'
import 'monaco-editor/languages/register.all.js'

export * from 'monaco-editor/editor.js'
