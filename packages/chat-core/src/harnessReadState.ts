/**
 * Read state of harness sessions, shared by the web and native clients.
 *
 * A finished run marks its session unread. The client showing a session
 * clears that marker whenever it learns the session is unread: when it opens
 * the session, when the run finishes while it is shown, and when it
 * reconciles the shown session after a reconnect or a return to the
 * foreground. A deliberate manual-unread marker stays until the user clears it.
 */

import type { HarnessSession } from "./harness";

type ReadState = Pick<HarnessSession, "status" | "unread" | "manual_unread">;

/** Whether the client showing `session` should mark it read now. */
export function shouldMarkViewedSessionRead(
  session: ReadState | null | undefined,
): boolean {
  return (
    session?.status === "idle" &&
    session.unread === true &&
    !session.manual_unread
  );
}
