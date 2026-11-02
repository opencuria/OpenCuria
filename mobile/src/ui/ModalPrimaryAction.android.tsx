import React from "react";
import { View } from "react-native";
import {
  ExtendedFloatingActionButton,
  Host,
  Icon,
  Text,
} from "@expo/ui/jetpack-compose";
import { semantics, testID } from "@expo/ui/jetpack-compose/modifiers";
import { androidIcon } from "./androidIcons";
import { Button } from "./controls";
import type { ModalPrimaryAction as Action } from "./ModalFrame.types";
import { useTheme } from "./theme";

/**
 * Material places a screen's main action at the bottom: an extended floating
 * action button for "+"-style actions, a filled button for confirmations.
 */
export function ModalPrimaryAction({
  action,
}: {
  action: Action;
}): React.JSX.Element {
  const { theme } = useTheme();
  if (!action.symbol)
    return (
      <View style={{ paddingHorizontal: 20, paddingVertical: 12 }}>
        <Button
          title={action.title}
          variant="primary"
          busy={action.busy}
          disabled={action.disabled}
          onPress={action.onPress}
          testID={action.testID}
        />
      </View>
    );
  return (
    <View
      pointerEvents="box-none"
      style={{ position: "absolute", right: 16, bottom: 16 }}
    >
      <Host matchContents colorScheme={theme.scheme} seedColor={theme.primary}>
        <ExtendedFloatingActionButton
          onClick={action.disabled ? undefined : action.onPress}
          modifiers={[
            semantics({ contentDescription: action.title }),
            ...(action.testID ? [testID(action.testID)] : []),
          ]}
        >
          <ExtendedFloatingActionButton.Icon>
            <Icon source={androidIcon(action.symbol)!} size={24} />
          </ExtendedFloatingActionButton.Icon>
          <ExtendedFloatingActionButton.Text>
            <Text>{action.title}</Text>
          </ExtendedFloatingActionButton.Text>
        </ExtendedFloatingActionButton>
      </Host>
    </View>
  );
}
