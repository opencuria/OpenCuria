import type React from "react";

export interface MediaViewerFrameProps {
  /** Shown in the navigation bar, e.g. the picture's caption. */
  title: string;
  /** Tapping the picture hides the bars for an unobstructed view. */
  chromeVisible: boolean;
  onBack: () => void;
  onShare: () => void;
  shareDisabled?: boolean;
  /** Full-bleed content; the bars float above it. */
  children: React.ReactNode;
}
