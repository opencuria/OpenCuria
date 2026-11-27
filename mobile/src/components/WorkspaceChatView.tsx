import React from "react";
import { StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  formatEffort,
  harnessEngineLabel,
} from "@opencuria/chat-core/harnessModels";
import { EMPTY_CHAT, type WorkspaceChat } from "../state/useWorkspaceChat";
import { FileViewerSheet } from "../tools/files";
import { useChromeTopInset } from "../ui/chromeInset";
import { Banner, NativeModal } from "../ui/primitives";
import { pickDocuments, pickPhoto } from "../platform";
// From the barrel, like the screens that render this view.
import { ChatComposer, ChatLayout, ChatMessages } from ".";
import type { ComposerWorkspace } from "./ChatComposer";
import type { ChatMessagesProps } from "./ChatMessages";
import { ModelSheet } from "./ModelSheet";

export interface WorkspaceChatViewProps {
  chat: WorkspaceChat;
  /** Until its first message, a new chat may still change workspace. */
  composerWorkspace?: ComposerWorkspace;
  /** Shown in an empty chat instead of the greeting, e.g. the setup guide. */
  emptyContent?: React.ReactNode;
  /** Offline, without a chat open. */
  emptyText?: string;
  renderPublishedApp?: ChatMessagesProps["renderPublishedApp"];
  onPullUpAtEnd?: () => void;
  pullUpLabel?: string;
  /** Opens Settings on a page; the model sheet links to providers. */
  onOpenSettings?: (path: string[]) => void;
  /** Reconnects while offline. */
  onRetryConnection?: () => void;
  /** Whether a reconnect attempt runs while offline. */
  reconnecting?: boolean;
  /** Message of the offline notice. */
  offlineMessage?: string;
  /** More status banners below the header, e.g. the app companion's. */
  notices?: React.ReactNode;
  /**
   * Renders the chat's sheets (model, rename, file). False when the host
   * renders `WorkspaceChatSheets` itself, so they also open while this view
   * is not on screen (e.g. renaming from a chat list beside it).
   */
  sheets?: boolean;
  /** Changes when the session changes; resets the transcript. */
  scopeKey: string;
}

/**
 * The chat of a workspace: transcript, composer with its sheets, gates and
 * dictation, status banners, model choice, renaming and the file viewer.
 * OpenCuria's main screen and the app companion render exactly this.
 */
export function WorkspaceChatView({
  chat,
  composerWorkspace,
  emptyContent,
  emptyText,
  renderPublishedApp,
  onPullUpAtEnd,
  pullUpLabel,
  onOpenSettings,
  onRetryConnection,
  reconnecting = false,
  offlineMessage = "You’re offline. Saved chats and installed apps still open.",
  notices,
  sheets = true,
  scopeKey,
}: WorkspaceChatViewProps): React.JSX.Element {
  const {
    controller,
    offline,
    activeEdit,
    navigationReady,
    steering,
    chatUnavailable,
  } = chat;
  const snapshot = chat.chat;
  return (
    <>
      <ChatLayout
        composer={
          <ChatComposer
            draft={activeEdit ? activeEdit.text : chat.draft}
            onDraftChange={(value) => chat.setComposerText(value)}
            onSend={activeEdit ? chat.submitEdit : chat.send}
            onSchedule={offline || activeEdit ? undefined : chat.scheduleDraft}
            editing={
              activeEdit && !activeEdit.sending
                ? {
                    id: activeEdit.messageId,
                    onCancel: () => chat.setEditing(null),
                  }
                : undefined
            }
            onStop={async () => {
              // Stopping drops queued messages; give them back to the draft.
              const restored = (await controller?.stop()) ?? [];
              chat.restoreToDraft(restored);
            }}
            busy={navigationReady && snapshot.busy}
            // An edit restarts the chat; it never steers a running turn.
            steering={steering && !activeEdit}
            // Offline a prompt can be written, not sent.
            disabled={offline ? false : chatUnavailable}
            canSend={!offline}
            missingModel={chat.missingModel}
            placeholder={
              steering && !activeEdit ? "Steer the agent…" : "Ask OpenCuria"
            }
            workspace={composerWorkspace}
            settings={
              offline
                ? undefined
                : {
                    harness: harnessEngineLabel(chat.harness, chat.engines),
                    mode: chat.mode,
                    model: chat.selectedModel?.name ?? chat.model,
                    effort: chat.effort ? formatEffort(chat.effort) : "",
                  }
            }
            onSettings={offline ? undefined : () => chat.openSheet("settings")}
            skills={chat.skills.map((item) => ({
              id: item.id,
              name: item.name,
            }))}
            selectedSkillIds={chat.skillIds}
            onSelectedSkillIdsChange={chat.setSkillIds}
            attachments={activeEdit ? activeEdit.attachments : chat.attachments}
            onAttachmentsChange={chat.setComposerAttachments}
            onSearchMentions={offline ? undefined : chat.searchMentions}
            onTakePhoto={async () => pickPhoto("camera")}
            onChoosePhotos={async () => pickPhoto("library")}
            onChooseFiles={async () => pickDocuments()}
            contextKey={chat.contextKey}
            voice={chat.voice}
            gates={{
              questions: navigationReady
                ? snapshot.questions
                : EMPTY_CHAT.questions,
              permissions: navigationReady
                ? snapshot.permissions
                : EMPTY_CHAT.permissions,
              submitting: snapshot.questions.some((item) =>
                chat.resolving.has(item.request_id),
              ),
              resolving: snapshot.permissions.some((item) =>
                chat.resolving.has(item.request_id),
              ),
              onSubmitQuestion: (id, answers) =>
                chat.gateAction(id, () =>
                  controller!.resolveQuestion(id, answers),
                ),
              onSkipQuestion: (id) =>
                chat.gateAction(id, () =>
                  controller!.resolveQuestion(id, [], true),
                ),
              onResolvePermission: (id, response) =>
                chat.gateAction(id, () =>
                  controller!.resolvePermission(id, response),
                ),
            }}
            queue={chat.outgoing}
            notice={chat.notice}
            testID="chat-composer"
          />
        }
        overlay={
          <ChromeOverlay onHeightChange={chat.setNoticeInset}>
            {offline && !chat.offlineNoticeHidden ? (
              <View style={styles.notice}>
                <Banner
                  tone="neutral"
                  message={offlineMessage}
                  actionLabel={
                    reconnecting || !onRetryConnection ? undefined : "Retry"
                  }
                  onAction={onRetryConnection}
                  onDismiss={() => chat.setOfflineNoticeHidden(true)}
                  testID="offline-notice"
                />
              </View>
            ) : null}
            {chat.error || snapshot.error ? (
              <View style={styles.notice}>
                <Banner
                  tone="error"
                  message={chat.error || snapshot.error || ""}
                  actionLabel="Retry"
                  onAction={() => {
                    void chat.refresh();
                  }}
                  onDismiss={() => {
                    chat.setError(null);
                    controller?.dismissError();
                  }}
                  testID="chat-error"
                />
              </View>
            ) : null}
            {notices ? <View style={styles.notice}>{notices}</View> : null}
          </ChromeOverlay>
        }
      >
        <ChatMessages
          key={`${scopeKey}:${chat.navigation?.revision ?? 0}`}
          loading={
            chat.loading ||
            (chat.workspaceId !== null &&
              !navigationReady &&
              !chat.error &&
              !snapshot.error)
          }
          messages={navigationReady ? snapshot.messages : EMPTY_CHAT.messages}
          sessions={snapshot.sessions}
          todos={snapshot.todos}
          busy={navigationReady && snapshot.busy}
          disabled={!chat.ready}
          resolveMedia={chat.resolveMedia}
          mediaScopeKey={`${chat.contextKey}:${snapshot.connected}:${offline}`}
          contentInsetTop={chat.noticeInset}
          emptyText={emptyText}
          emptyContent={emptyContent}
          // Subtasks take no prompts from the user.
          onEdit={offline || chat.activeIsSubtask ? undefined : chat.startEdit}
          editingMessageId={activeEdit?.messageId ?? null}
          onFork={
            offline
              ? undefined
              : async (messageId) => {
                  await controller?.fork(messageId);
                  chat.setError(null);
                }
          }
          onOpenSubtask={(id) => {
            chat.setAttachments([]);
            chat.setSkillIds([]);
            if (chat.workspaceId)
              chat.requestNavigation(chat.workspaceId, { sessionId: id });
          }}
          onRequestPartDetail={(sessionId, partId) =>
            offline
              ? Promise.reject(new Error("Details need a connection."))
              : controller!.requestPartDetail(sessionId, partId)
          }
          onOpenFile={offline ? undefined : (path) => chat.openFile(path)}
          onOpenAttachment={(attachment) => chat.openFile(attachment)}
          onPullUpAtEnd={onPullUpAtEnd}
          pullUpLabel={pullUpLabel}
          renderPublishedApp={renderPublishedApp}
          testID="chat-transcript"
        />
      </ChatLayout>

      {sheets ? (
        <WorkspaceChatSheets chat={chat} onOpenSettings={onOpenSettings} />
      ) : null}
    </>
  );
}

/**
 * The chat's sheets: model choice, renaming and the file viewer. Part of
 * `WorkspaceChatView`, or rendered by a host that shows other pages beside it.
 */
export function WorkspaceChatSheets({
  chat,
  onOpenSettings,
}: Pick<WorkspaceChatViewProps, "chat" | "onOpenSettings">): React.JSX.Element {
  const insets = useSafeAreaInsets();
  const { client } = chat;
  return (
    <>
      <ModelSheet
        visible={chat.sheet === "settings"}
        onClose={() => chat.closeSheet("settings")}
        models={chat.modelsForEngine}
        recentModelIds={chat.recentModels.map((row) => row.model)}
        model={chat.model}
        effort={chat.effort}
        engines={chat.engineOptions}
        engine={chat.harness}
        mode={chat.mode}
        unavailableMessage={
          chat.catalogError ??
          `${harnessEngineLabel(chat.harness, chat.engines)} models unavailable. Check the provider account in Settings.`
        }
        onUnavailablePress={
          onOpenSettings ? () => onOpenSettings(["providers"]) : undefined
        }
        onModelChange={chat.selectModel}
        onEffortChange={chat.selectEffort}
        onAgentChange={chat.selectAgent}
      />

      <NativeModal
        visible={chat.sheet === "rename"}
        onClose={() => chat.setSheet(null)}
        title="Rename conversation"
        testID="rename-session-sheet"
        primaryAction={{
          title: "Save",
          onPress: () => {
            void chat.saveRename();
          },
          disabled: !chat.renameText.trim(),
          busy: chat.renaming,
          testID: "rename-save",
        }}
        form={{
          sections: [
            {
              id: "rename",
              rows: [
                {
                  kind: "text",
                  id: "rename-title",
                  label: "Name",
                  value: chat.renameText,
                  onChange: chat.setRenameText,
                  autoFocus: true,
                  onSubmit: () => {
                    void chat.saveRename();
                  },
                },
              ],
            },
          ],
        }}
      />
      {client && chat.workspace && chat.sheet === "file" ? (
        <FileViewerSheet
          key={chat.contextKey}
          client={client}
          workspaceId={chat.workspace.id}
          media={chat.media}
          file={chat.file}
          bottomInset={insets.bottom}
          onInsertIntoChat={chat.insertIntoChat}
          onClose={chat.closeFile}
        />
      ) : null}
    </>
  );
}

/**
 * Status banners float just below the translucent header; the transcript pads
 * its first message by their measured height so nothing stays hidden.
 */
function ChromeOverlay({
  children,
  onHeightChange,
}: React.PropsWithChildren<{
  onHeightChange: (height: number) => void;
}>): React.JSX.Element {
  const top = useChromeTopInset();
  return (
    <View
      pointerEvents="box-none"
      onLayout={(event) => onHeightChange(event.nativeEvent.layout.height)}
      style={[styles.overlay, { top }]}
    >
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  notice: { paddingHorizontal: 12, paddingTop: 7 },
  overlay: { position: "absolute", left: 0, right: 0, zIndex: 3 },
});
