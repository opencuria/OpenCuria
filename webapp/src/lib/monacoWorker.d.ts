declare module '*?monaco-worker' {
  const WorkerConstructor: { new(options?: WorkerOptions): Worker }
  export default WorkerConstructor
}
