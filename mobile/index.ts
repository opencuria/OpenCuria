import "expo-router/entry";
import { AppRegistry } from "react-native";
import {
  APP_COMPANION_COMPONENT,
  AppCompanionRoot,
} from "./src/apps/companion/AppCompanionRoot";

// The companion drawn over an open app (`modules/opencuria-app-runtime`).
AppRegistry.registerComponent(APP_COMPANION_COMPONENT, () => AppCompanionRoot);
