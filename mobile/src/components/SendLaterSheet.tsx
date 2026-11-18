import React, { useState } from "react";
import {
  formatSendTime,
  sendLaterDefault,
  sendLaterError,
  sendLaterFields,
  sendLaterFromFields,
} from "@opencuria/chat-core/sendLater";
import { NativeModal } from "../ui/primitives";

export interface SendLaterSheetProps {
  visible: boolean;
  /** Preselected time; defaults to the next full hour. */
  initial?: Date | null;
  /** Primary action, e.g. "Schedule" or "Reschedule". */
  actionTitle?: string;
  onClose: () => void;
  onConfirm: (sendAt: Date) => void;
}

/**
 * Native date and time pickers for "Send later": a SwiftUI form in a page
 * sheet on iOS, Material date and time dialogs on Android.
 */
export function SendLaterSheet(
  props: SendLaterSheetProps,
): React.JSX.Element | null {
  if (!props.visible) return null;
  // A fresh picker per presentation starts from its own initial time.
  return <SendLaterForm {...props} />;
}

function SendLaterForm({
  initial,
  actionTitle = "Schedule",
  onClose,
  onConfirm,
}: SendLaterSheetProps): React.JSX.Element {
  const [fields, setFields] = useState(() =>
    sendLaterFields(
      initial && !sendLaterError(initial) ? initial : sendLaterDefault(),
    ),
  );
  const at = sendLaterFromFields(fields.date, fields.time);
  const error = sendLaterError(at);
  const today = sendLaterFields(new Date()).date;
  return (
    <NativeModal
      visible
      onClose={onClose}
      title="Send later"
      primaryAction={{
        title: actionTitle,
        disabled: Boolean(error),
        onPress: () => {
          if (at && !sendLaterError(at)) onConfirm(at);
        },
        testID: "send-later-confirm",
      }}
      form={{
        sections: [
          {
            id: "when",
            rows: [
              {
                kind: "date",
                id: "send-later-date",
                label: "Date",
                mode: "date",
                value: fields.date,
                minimumDate: today,
                onChange: (date) =>
                  setFields((current) => ({ ...current, date })),
              },
              {
                kind: "date",
                id: "send-later-time",
                label: "Time",
                mode: "time",
                value: fields.time,
                onChange: (time) =>
                  setFields((current) => ({ ...current, time })),
              },
            ],
            footer:
              error ??
              `Sends ${formatSendTime(at!)}. A busy agent receives it while it works.`,
            footerTone: error ? "danger" : undefined,
          },
        ],
      }}
      testID="send-later-sheet"
    />
  );
}
