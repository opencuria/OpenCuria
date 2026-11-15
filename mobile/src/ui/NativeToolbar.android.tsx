import { requireNativeView } from "expo";
import type { ViewProps } from "react-native";

/** One toolbar action: an overflow entry, or an icon in the bar when `icon` is set. */
export interface NativeToolbarMenuItem {
  id: string;
  title: string;
  enabled: boolean;
  checked: boolean;
  /** Bundled action icon shown directly in the bar. */
  icon?: "share";
}

export interface NativeToolbarProps extends ViewProps {
  title: string;
  subtitle: string;
  titleColor: string;
  subtitleColor: string;
  surfaceColor: string;
  tintColor: string;
  showClose: boolean;
  /** Navigation icon: dismiss (×) for dialogs, back (←) for viewers. */
  navigationIcon?: "close" | "back";
  menuItems: NativeToolbarMenuItem[];
  onClose: () => void;
  onMenuAction: (event: { nativeEvent: { id: string } }) => void;
}

/** Android's AppCompat `Toolbar` from the local `opencuria-native-ui` module. */
export const NativeToolbar =
  requireNativeView<NativeToolbarProps>("OpenCuriaNativeUI");
