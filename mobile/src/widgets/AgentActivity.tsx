import { HStack, Image, Spacer, Text, VStack } from "@expo/ui/swift-ui";
import {
  font,
  foregroundStyle,
  lineLimit,
  padding,
  widgetURL,
} from "@expo/ui/swift-ui/modifiers";
import { createLiveActivity, type LiveActivityEnvironment } from "expo-widgets";
import type { AgentActivitySnapshot } from "@opencuria/client";

/** Pure SwiftUI layout, compiled into Expo's isolated widget extension. */
function layout(
  props: AgentActivitySnapshot,
  environment: LiveActivityEnvironment,
) {
  "widget";
  const rows = props.activities ?? [];
  const attention = rows.find(
    (row) =>
      row.phase === "waiting_for_approval" || row.phase === "waiting_for_input",
  );
  const row = attention ?? rows[0];
  const stale = environment.isStale;
  const finished = props.activeCount === 0;
  const failed = row?.phase === "error";
  const label = stale
    ? "Status out of date"
    : attention
      ? "Needs your attention"
      : finished
        ? failed
          ? "Agent run failed"
          : row?.phase === "interrupted"
            ? "Agent stopped"
            : "Agent finished"
        : props.activeCount === 1
          ? "Agent is working"
          : `${props.activeCount} agents working`;
  const color = failed ? "#F87171" : attention ? "#FBBF24" : "#A3B6FF";
  const glyph = attention
    ? "exclamationmark.circle.fill"
    : finished
      ? failed
        ? "xmark.circle.fill"
        : "checkmark.circle.fill"
      : "circle.dashed";
  const link = row
    ? `opencuria:///?deviceId=${props.deviceId}&organizationId=${props.organizationId}&workspaceId=${row.workspaceId}&sessionId=${row.sessionId}`
    : "opencuria:///";
  const links = [widgetURL(link)];
  const status = (
    <HStack spacing={8} modifiers={links}>
      <Image systemName={glyph} size={18} color={color} />
      <Text
        modifiers={[
          font({ size: 14, weight: "semibold" }),
          foregroundStyle("#FFFFFF"),
          lineLimit(1),
        ]}
      >
        {label}
      </Text>
      <Spacer />
      <Text
        modifiers={[
          font({ size: 11, weight: "semibold" }),
          foregroundStyle("#A3A3AD"),
        ]}
      >
        OpenCuria
      </Text>
    </HStack>
  );
  return {
    banner: (
      <VStack
        alignment="leading"
        spacing={8}
        modifiers={[padding({ all: 16 }), ...links]}
      >
        {status}
        {rows.slice(0, 3).map((item) => (
          <HStack key={item.sessionId} spacing={8}>
            <Text
              modifiers={[
                font({ size: 13, weight: "medium" }),
                foregroundStyle("#F4F4F6"),
                lineLimit(1),
              ]}
            >
              {item.title}
            </Text>
            <Spacer />
            <Text
              modifiers={[
                font({ size: 11 }),
                foregroundStyle("#A3A3AD"),
                lineLimit(1),
              ]}
            >
              {item.workspaceName}
            </Text>
          </HStack>
        ))}
      </VStack>
    ),
    compactLeading: <Image systemName={glyph} size={18} color={color} />,
    compactTrailing: (
      <Text
        modifiers={[
          font({ size: 13, weight: "semibold" }),
          foregroundStyle(color),
        ]}
      >
        {finished ? "✓" : `${props.activeCount}`}
      </Text>
    ),
    minimal: <Image systemName={glyph} size={16} color={color} />,
    expandedCenter: status,
    expandedBottom: (
      <Text
        modifiers={[
          font({ size: 13 }),
          foregroundStyle("#F4F4F6"),
          lineLimit(2),
          ...links,
        ]}
      >
        {row?.title ?? "Open OpenCuria"}
      </Text>
    ),
  };
}

export const AgentActivity = createLiveActivity(
  "OpenCuriaAgentActivity",
  layout,
);
