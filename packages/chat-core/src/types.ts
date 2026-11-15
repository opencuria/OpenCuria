/** Minimal file explorer DTO shared by framework-neutral chat helpers. */
export interface FileNode {
  name: string;
  path: string;
  type: "file" | "directory";
  size: number;
  children?: FileNode[];
  isExpanded?: boolean;
}
