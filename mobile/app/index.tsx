import React from "react";
import { ActivityIndicator, StyleSheet, View } from "react-native";
import { Banner, Button } from "../src/ui/primitives";
import { useTheme } from "../src/ui/theme";
import { LoginScreen } from "../src/screens/LoginScreen";
import { MainScreen } from "../src/screens/MainScreen";
import { useSession } from "../src/screens/SessionContext";

export default function IndexRoute(): React.JSX.Element {
  const { booting, session, user, error, sessionExpired, retry, signOut } =
    useSession();
  const { theme } = useTheme();
  if (booting && (!session || !user)) {
    return (
      <View style={[styles.loading, { backgroundColor: theme.background }]}>
        <ActivityIndicator color={theme.primary} />
      </View>
    );
  }
  if (sessionExpired || !session) return <LoginScreen />;
  if (error || !user) {
    return (
      <View style={[styles.errorScreen, { backgroundColor: theme.background }]}>
        <Banner
          tone="error"
          message={error ?? "Could not load your profile."}
          testID="session-error"
        />
        <View style={styles.actions}>
          <Button
            title="Retry connection"
            variant="primary"
            onPress={() => {
              void retry();
            }}
            busy={booting}
            testID="session-retry"
          />
          <Button
            title="Sign out"
            variant="outline"
            onPress={() => {
              void signOut();
            }}
            testID="session-sign-out"
          />
        </View>
      </View>
    );
  }
  return <MainScreen />;
}

const styles = StyleSheet.create({
  loading: { flex: 1, justifyContent: "center", alignItems: "center" },
  errorScreen: {
    flex: 1,
    justifyContent: "center",
    paddingHorizontal: 22,
    gap: 18,
  },
  actions: {
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "center",
    gap: 12,
  },
});
