import React from "react";
import { Share } from "react-native";
import * as Clipboard from "expo-clipboard";
import type { ModalPrimaryAction } from "../../ui/ModalFrame.types";
import type {
  NativeFormSection,
  NativeFormTone,
} from "../../ui/NativeForm.types";
import { showToast } from "../../ui/toast";
import { loadErrorSections } from "../sections";
import { SettingsPage } from "../SettingsPage";

export interface TextPageProps {
  title: string;
  /** Label above the text, e.g. "Public key". */
  label: string;
  text: string | undefined;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  /** Code-like text in a monospaced font (default). */
  monospace?: boolean;
  /** Shown above the text, e.g. a one-time secret warning. */
  notice?: string;
  noticeTone?: NativeFormTone;
  footer?: string;
  /** Shown when the text is empty. */
  emptyText?: string;
  copyTitle?: string;
  primaryAction?: ModalPrimaryAction;
  testID?: string;
}

/** Read-only text such as a public key, token or build log, with Copy and Share. */
export function TextPage({
  title,
  label,
  text,
  loading,
  error,
  onRetry,
  monospace = true,
  notice,
  noticeTone = "warning",
  footer,
  emptyText = "Nothing to show.",
  copyTitle = "Copy",
  primaryAction,
  testID,
}: TextPageProps): React.JSX.Element {
  const sections: NativeFormSection[] =
    text === undefined
      ? []
      : [
          ...(notice
            ? [
                {
                  id: "notice",
                  rows: [
                    {
                      kind: "note" as const,
                      id: "notice",
                      text: notice,
                      tone: noticeTone,
                    },
                  ],
                },
              ]
            : []),
          {
            id: "text",
            footer,
            rows: [
              {
                kind: "value",
                id: "text",
                testID: "text-value",
                label,
                value: text || emptyText,
                monospace,
                stacked: !monospace,
              },
              ...(text
                ? [
                    {
                      kind: "button" as const,
                      id: "copy",
                      title: copyTitle,
                      symbol: "doc.on.doc" as const,
                      onPress: () => {
                        void Clipboard.setStringAsync(text);
                        showToast({ title: "Copied", tone: "success" });
                      },
                    },
                    {
                      kind: "button" as const,
                      id: "share",
                      title: "Share",
                      symbol: "square.and.arrow.up" as const,
                      onPress: () =>
                        void Share.share({ message: text }).catch(
                          () => undefined,
                        ),
                    },
                  ]
                : []),
            ],
          },
        ];
  return (
    <SettingsPage
      title={title}
      sections={
        error && text === undefined
          ? loadErrorSections(error, () => onRetry?.())
          : sections
      }
      loading={loading && text === undefined}
      primaryAction={primaryAction}
      testID={testID}
    />
  );
}
