import React from "react";
import { View } from "react-native";
import {
  Button,
  CircularProgressIndicator,
  ExtendedFloatingActionButton,
  Host,
  Icon,
  Shape,
  Spacer,
  Text,
} from "@expo/ui/jetpack-compose";
import {
  alpha,
  defaultMinSize,
  fillMaxWidth,
  semantics,
  size,
  testID,
  width,
} from "@expo/ui/jetpack-compose/modifiers";
import { androidIcon } from "./androidIcons";
import type { ModalPrimaryAction as Action } from "./ModalFrame.types";
import { useTheme } from "./theme";

/**
 * Material places a screen's main action at the bottom: an ink extended
 * floating action button for "+"-style actions, a full-width ink pill for
 * confirmations.
 */
export function ModalPrimaryAction({
  action,
}: {
  action: Action;
}): React.JSX.Element {
  const { theme } = useTheme();
  if (!action.symbol)
    return (
      <View
        testID={action.testID}
        style={{ paddingHorizontal: 20, paddingVertical: 12 }}
      >
        <Host
          matchContents={{ vertical: true }}
          colorScheme={theme.scheme}
          seedColor={theme.seed}
          ignoreSafeAreaKeyboardInsets
        >
          <Button
            onClick={action.onPress}
            enabled={!action.disabled && !action.busy}
            colors={{
              containerColor: theme.primary,
              contentColor: theme.primaryForeground,
              disabledContainerColor: theme.controlFill,
              disabledContentColor: theme.subtleText,
            }}
            shape={Shape.Pill({})}
            modifiers={[
              fillMaxWidth(),
              defaultMinSize({ minHeight: 52 }),
              semantics({ contentDescription: action.title }),
            ]}
          >
            {action.busy ? (
              <CircularProgressIndicator
                color={theme.subtleText}
                strokeWidth={2}
                modifiers={[size(18, 18)]}
              />
            ) : null}
            {action.busy ? <Spacer modifiers={[width(10)]} /> : null}
            <Text style={{ typography: "titleMedium", fontWeight: "600" }}>
              {action.title}
            </Text>
          </Button>
        </Host>
      </View>
    );
  return (
    <View
      pointerEvents="box-none"
      style={{ position: "absolute", right: 16, bottom: 16 }}
    >
      <Host matchContents colorScheme={theme.scheme} seedColor={theme.seed}>
        <ExtendedFloatingActionButton
          containerColor={theme.primary}
          onClick={action.disabled || action.busy ? undefined : action.onPress}
          modifiers={[
            semantics({ contentDescription: action.title }),
            ...(action.testID ? [testID(action.testID)] : []),
            ...(action.disabled ? [alpha(0.38)] : []),
          ]}
        >
          <ExtendedFloatingActionButton.Icon>
            <Icon
              source={androidIcon(action.symbol) ?? androidIcon("plus")!}
              tint={theme.primaryForeground}
              size={24}
            />
          </ExtendedFloatingActionButton.Icon>
          <ExtendedFloatingActionButton.Text>
            <Text
              color={theme.primaryForeground}
              style={{ typography: "labelLarge", fontWeight: "600" }}
            >
              {action.title}
            </Text>
          </ExtendedFloatingActionButton.Text>
        </ExtendedFloatingActionButton>
      </Host>
    </View>
  );
}
