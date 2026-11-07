import type { ReactElement } from "react";
import { PlatformColor } from "react-native";
import {
  Circle,
  HStack,
  Image,
  Link,
  Rectangle,
  Text,
  VStack,
  ZStack,
} from "@expo/ui/swift-ui";
import {
  aspectRatio,
  containerBackground,
  createModifier,
  font,
  foregroundStyle,
  frame,
  lineLimit,
  multilineTextAlignment,
  padding,
  redacted,
  resizable,
  truncationMode,
  widgetAccentedRenderingMode,
  widgetURL,
} from "@expo/ui/swift-ui/modifiers";
import { createWidget, type WidgetEnvironment } from "expo-widgets";
import type { AppsWidgetApp, AppsWidgetSnapshot } from "./appsWidgetSnapshot";

/**
 * The apps OpenCuria runs, as home screen icons in a group: as many as the
 * widget size holds, each opening its app. Pure SwiftUI layout, compiled
 * into Expo's widget extension, where only its arguments and the imported
 * components exist: every helper lives inside.
 */
function layout(
  props: Partial<AppsWidgetSnapshot>,
  environment: WidgetEnvironment,
) {
  "widget";
  const family = environment.widgetFamily;
  const large = family === "systemLarge" || family === "systemExtraLarge";
  const columns =
    family === "systemSmall" ? 2 : family === "systemExtraLarge" ? 8 : 4;
  const rows = large ? 4 : 2;
  // Home screen icons: about 60 pt, continuous corners of 22.4 %.
  const iconMax = large ? 62 : 60;
  const radius = large ? 13 : 11.5;
  const labelSize = large ? 12 : 11;
  const appsUrl = props.appsUrl ?? "opencuria:///";
  const label = PlatformColor("label");
  const secondaryLabel = PlatformColor("secondaryLabel");
  const fill = PlatformColor("tertiarySystemFill");
  const fullSize = frame({ maxWidth: Infinity, maxHeight: Infinity });
  const square = [
    aspectRatio({ ratio: 1, contentMode: "fit" }),
    frame({ maxWidth: iconMax, maxHeight: iconMax }),
  ];
  const clip = (cornerRadius: number) =>
    createModifier("clipShape", {
      shape: "roundedRectangle",
      cornerRadius,
      roundedCornerStyle: "continuous",
    });
  // A hairline keeps light icons apart from the background, as in the App Store.
  const outline = (cornerRadius: number) =>
    createModifier("strokeBorder", {
      content: { type: "color", color: PlatformColor("quaternaryLabel") },
      style: { lineWidth: 0.5 },
      shape: "roundedRectangle",
      cornerRadius,
      roundedCornerStyle: "continuous",
    });
  const background = containerBackground(
    PlatformColor("secondarySystemGroupedBackground"),
    "widget",
  );
  const empty = () => (
    <Rectangle modifiers={[foregroundStyle("clear"), fullSize]} />
  );

  // The app's icon; apps without one get OpenCuria's app tile.
  const icon = (app: AppsWidgetApp, cornerRadius: number, glyph: number) =>
    app.icon ? (
      <Image
        uiImage={app.icon}
        modifiers={[
          resizable(),
          // Tinted and clear home screens tint it like every other icon.
          widgetAccentedRenderingMode("accentedDesaturated"),
          aspectRatio({ ratio: 1, contentMode: "fit" }),
          clip(cornerRadius),
          outline(cornerRadius),
        ]}
      />
    ) : (
      <ZStack modifiers={[aspectRatio({ ratio: 1, contentMode: "fit" })]}>
        <Rectangle
          modifiers={[
            foregroundStyle(PlatformColor("systemIndigo")),
            clip(cornerRadius),
          ]}
        />
        <Image systemName="square.grid.2x2.fill" size={glyph} color="white" />
      </ZStack>
    );

  const name = (text: string, isNew: boolean) => (
    <HStack spacing={3}>
      {isNew ? (
        <Circle
          modifiers={[
            foregroundStyle(PlatformColor("systemBlue")),
            frame({ width: 6, height: 6 }),
          ]}
        />
      ) : null}
      <Text
        modifiers={[
          font({ size: labelSize }),
          foregroundStyle(label),
          lineLimit(1),
          truncationMode("tail"),
        ]}
      >
        {text}
      </Text>
    </HStack>
  );

  const tile = (app: AppsWidgetApp) => (
    <Link destination={app.url}>
      <VStack spacing={4} modifiers={[fullSize]}>
        <ZStack modifiers={square}>{icon(app, radius, large ? 26 : 22)}</ZStack>
        {name(app.name, app.isNew)}
      </VStack>
    </Link>
  );

  // Apps that do not fit, previewed like a folder; opens the Apps list.
  const more = (rest: AppsWidgetApp[]) => {
    const mini = (app: AppsWidgetApp | undefined) =>
      app ? icon(app, 3.5, 7) : empty();
    return (
      <Link destination={appsUrl}>
        <VStack spacing={4} modifiers={[fullSize]}>
          <ZStack modifiers={square}>
            <Rectangle modifiers={[foregroundStyle(fill), clip(radius)]} />
            <VStack spacing={4} modifiers={[padding({ all: 7 })]}>
              <HStack spacing={4}>
                {mini(rest[0])}
                {mini(rest[1])}
              </HStack>
              <HStack spacing={4}>
                {mini(rest[2])}
                {mini(rest[3])}
              </HStack>
            </VStack>
          </ZStack>
          {name("More", false)}
        </VStack>
      </Link>
    );
  };

  const placeholder = () => (
    <VStack spacing={4} modifiers={[fullSize]}>
      <Rectangle modifiers={[foregroundStyle(fill), ...square, clip(radius)]} />
      <Text modifiers={[font({ size: labelSize }), redacted("placeholder")]}>
        App
      </Text>
    </VStack>
  );

  const grid = (cells: ReactElement[]) => (
    <VStack
      spacing={large ? 6 : 2}
      modifiers={[
        padding({ horizontal: 8, vertical: large ? 14 : 10 }),
        fullSize,
        background,
        widgetURL(appsUrl),
      ]}
    >
      {Array.from({ length: rows }, (_, row) => (
        <HStack key={row} spacing={0} modifiers={[fullSize]}>
          {Array.from(
            { length: columns },
            (_, column) => cells[row * columns + column] ?? empty(),
          )}
        </HStack>
      ))}
    </VStack>
  );

  const message = (title: string, detail: string | null) => (
    <VStack
      spacing={6}
      modifiers={[
        padding({ all: 16 }),
        fullSize,
        background,
        widgetURL(appsUrl),
      ]}
    >
      <Image
        systemName="square.grid.2x2"
        size={large ? 34 : 26}
        color={secondaryLabel}
      />
      <Text
        modifiers={[
          font({ size: 15, weight: "semibold" }),
          foregroundStyle(label),
          multilineTextAlignment("center"),
        ]}
      >
        {title}
      </Text>
      {detail && family !== "systemSmall" ? (
        <Text
          modifiers={[
            font({ size: 13 }),
            foregroundStyle(secondaryLabel),
            multilineTextAlignment("center"),
          ]}
        >
          {detail}
        </Text>
      ) : null}
    </VStack>
  );

  const slots = columns * rows;
  // Before OpenCuria first wrote the widget (e.g. in the widget gallery).
  if (!props.status)
    return grid(Array.from({ length: slots }, () => placeholder()));
  if (props.status === "signedOut")
    return message("Sign in to see your apps", null);
  const apps = props.apps ?? [];
  if (!apps.length)
    return message("No apps yet", "Apps you install in OpenCuria appear here.");
  // The most recently used apps that fit, in a stable alphabetical order.
  const overflow = apps.length > slots;
  const shown = apps
    .slice(0, overflow ? slots - 1 : slots)
    .sort((a, b) => a.order - b.order);
  const cells = shown.map((app) => tile(app));
  if (overflow) cells.push(more(apps.slice(slots - 1)));
  return grid(cells);
}

export const AppsWidget = createWidget<Partial<AppsWidgetSnapshot>>(
  "OpenCuriaApps",
  layout,
);
