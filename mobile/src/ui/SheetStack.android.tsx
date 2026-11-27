import React, { useCallback, useEffect, useRef, useState } from "react";
import { StyleSheet, View, type LayoutChangeEvent } from "react-native";
import Animated, { SlideInRight, SlideOutRight } from "react-native-reanimated";
import { ArrowLeft, Ellipsis } from "lucide-react-native";
import { ChromeInsetContext } from "./chromeInset";
import { IconButton } from "./controls";
import { NativeMenu } from "./NativeMenu";
import { NavigationTitle } from "./NavigationTitle";
import type { SheetPage, SheetStackProps } from "./SheetStack.types";
import { useTheme } from "./theme";

const PUSH_MS = 240;
const POP_MS = 200;

/**
 * The pages of a sheet with Material controls, like the chat's header:
 * floating bars above content that scrolls beneath them, the detail page
 * sliding in over the root. System Back is the host's, which knows whether
 * the sheet is on screen.
 */
export function SheetStack({
  root,
  detail,
  detailShown,
  onDetailShownChange,
  onBarHeight,
}: SheetStackProps): React.JSX.Element {
  const { theme } = useTheme();
  const showDetail = detailShown && detail != null;
  // A page shown from the start does not slide in.
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
  }, []);
  const latest = useRef(onBarHeight);
  latest.current = onBarHeight;
  const reportBarHeight = useCallback(
    (height: number) => latest.current?.(height),
    [],
  );
  return (
    <View style={styles.root}>
      <View
        style={styles.root}
        importantForAccessibility={showDetail ? "no-hide-descendants" : "auto"}
      >
        <SheetScreen page={root} onBarHeight={reportBarHeight} />
      </View>
      {showDetail ? (
        <Animated.View
          entering={
            mounted.current ? SlideInRight.duration(PUSH_MS) : undefined
          }
          exiting={SlideOutRight.duration(POP_MS)}
          style={[
            StyleSheet.absoluteFill,
            { backgroundColor: theme.background },
          ]}
        >
          <SheetScreen
            page={detail}
            onBack={() => onDetailShownChange(false)}
            onBarHeight={reportBarHeight}
          />
        </Animated.View>
      ) : null}
    </View>
  );
}

function SheetScreen({
  page,
  onBack,
  onBarHeight,
}: {
  page: SheetPage;
  onBack?: () => void;
  onBarHeight: (height: number) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const [barHeight, setBarHeight] = useState(0);
  const onLayout = (event: LayoutChangeEvent) => {
    const height = Math.round(event.nativeEvent.layout.height);
    setBarHeight(height);
    onBarHeight(height);
  };
  const chrome = [styles.chrome, { backgroundColor: theme.surfaceMuted }];
  const actions = page.actions ?? [];
  const menuActions =
    page.menu?.sections.flatMap((section) => section.actions) ?? [];
  return (
    <View style={styles.root} testID={page.testID}>
      <ChromeInsetContext.Provider value={barHeight}>
        <View style={styles.root}>{page.children}</View>
      </ChromeInsetContext.Provider>
      <View
        onLayout={onLayout}
        pointerEvents="box-none"
        style={[
          styles.bar,
          {
            experimental_backgroundImage: `linear-gradient(to bottom, ${theme.background} 45%, ${theme.background}00 100%)`,
          },
        ]}
      >
        {/* Back and title share one floating bar, legible above the content. */}
        <View
          style={[chrome, styles.titleBar, onBack ? null : styles.titleOnly]}
        >
          {onBack ? (
            <IconButton
              label="Back"
              icon={<ArrowLeft color={theme.text} size={22} />}
              onPress={onBack}
              testID={page.testID ? `${page.testID}-back` : undefined}
            />
          ) : null}
          <NavigationTitle
            title={page.title}
            subtitle={page.subtitle}
            testID={page.testID ? `${page.testID}-title` : undefined}
          />
        </View>
        {actions.length || menuActions.length ? (
          <View style={[chrome, styles.group]}>
            {actions.map((action) => {
              const Icon = action.icon;
              return (
                <IconButton
                  key={action.id}
                  label={action.label}
                  accessibilityHint={action.accessibilityHint}
                  icon={<Icon color={action.tint ?? theme.text} size={22} />}
                  disabled={action.disabled}
                  onPress={action.onPress}
                  testID={action.testID}
                />
              );
            })}
            {page.menu && menuActions.length ? (
              <NativeMenu
                label={page.menu.label}
                actions={menuActions}
                onSelect={page.menu.onSelect}
                style={styles.menu}
                testID={page.menu.testID}
              >
                <View style={styles.menuIcon}>
                  <Ellipsis color={theme.text} size={22} />
                </View>
              </NativeMenu>
            ) : null}
          </View>
        ) : null}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  bar: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 12,
    paddingBottom: 12,
  },
  chrome: { borderRadius: 24, overflow: "hidden" },
  titleBar: {
    flexDirection: "row",
    alignItems: "center",
    flexShrink: 1,
    marginEnd: "auto",
    minHeight: 48,
    paddingEnd: 20,
  },
  titleOnly: { paddingStart: 16 },
  group: { flexDirection: "row" },
  menu: { width: 48, height: 48 },
  menuIcon: {
    width: 48,
    height: 48,
    alignItems: "center",
    justifyContent: "center",
  },
});
