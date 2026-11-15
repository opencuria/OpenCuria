import React, { memo } from "react";
import { SvgXml } from "react-native-svg";
import {
  DEFAULT_FILE_ICON,
  resolveWorkspaceIconKey,
} from "@opencuria/chat-core/fileIcons";
import { FILE_ICON_SVGS } from "./fileIconSvgs.generated";

export interface WorkspaceFileIconProps {
  path: string;
  kind?: "file" | "directory";
  expanded?: boolean;
  size?: number;
}

/** The webapp's VS Code-like file and folder icons, drawn natively. */
export const WorkspaceFileIcon = memo(function WorkspaceFileIcon({
  path,
  kind = "file",
  expanded = false,
  size = 16,
}: WorkspaceFileIconProps): React.JSX.Element {
  const key = resolveWorkspaceIconKey({
    path,
    directory: kind === "directory",
    expanded,
  });
  const xml = FILE_ICON_SVGS[key] ?? FILE_ICON_SVGS[DEFAULT_FILE_ICON]!;
  return (
    <SvgXml
      xml={xml}
      width={size}
      height={size}
      accessibilityElementsHidden
      importantForAccessibility="no"
    />
  );
});
