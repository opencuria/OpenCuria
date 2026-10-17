import React, { useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { LockKeyhole, Server } from "lucide-react-native";
import { Banner, Button } from "../ui/primitives";
import { useTheme } from "../ui/theme";
import { OpenCuriaMark } from "../ui/OpenCuriaMark";
import { useSession } from "./SessionContext";

const DEFAULT_SERVER_URL = process.env.EXPO_PUBLIC_DEFAULT_SERVER_URL ?? "";

export function LoginScreen(): React.JSX.Element {
  const { theme } = useTheme();
  const { login, error, clearError, booting } = useSession();
  const [serverUrl, setServerUrl] = useState(DEFAULT_SERVER_URL);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (submitting) return;
    setSubmitting(true);
    clearError();
    try {
      await login(serverUrl, email, password);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={[styles.root, { backgroundColor: theme.background }]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView
        keyboardShouldPersistTaps="handled"
        contentContainerStyle={styles.content}
      >
        <View
          style={[styles.brandMark, { backgroundColor: theme.surfaceMuted }]}
        >
          <OpenCuriaMark size={36} />
        </View>
        <Text
          accessibilityRole="header"
          style={[styles.title, { color: theme.text }]}
        >
          Welcome to OpenCuria
        </Text>
        <Text style={[styles.subtitle, { color: theme.mutedText }]}>
          Connect to your workspace and let your agents get to work.
        </Text>

        {error ? (
          <Banner
            tone="error"
            message={error}
            testID="login-error"
            style={{ width: "100%" }}
          />
        ) : null}
        <View style={styles.fields}>
          <Text style={[styles.label, { color: theme.mutedText }]}>
            Server URL
          </Text>
          <View
            style={[
              styles.inputWrap,
              { backgroundColor: theme.surface, borderColor: theme.border },
            ]}
          >
            <Server size={18} color={theme.subtleText} />
            <TextInput
              value={serverUrl}
              onChangeText={setServerUrl}
              onFocus={clearError}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholder="https://curia.example"
              placeholderTextColor={theme.subtleText}
              accessibilityLabel="Server URL"
              testID="login-server"
              style={[styles.input, { color: theme.text }]}
            />
          </View>
          <Text
            style={[styles.label, { color: theme.mutedText, marginTop: 4 }]}
          >
            Email
          </Text>
          <View
            style={[
              styles.inputWrap,
              { backgroundColor: theme.surface, borderColor: theme.border },
            ]}
          >
            <TextInput
              value={email}
              onChangeText={setEmail}
              onFocus={clearError}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="email-address"
              textContentType="username"
              placeholder="you@example.com"
              placeholderTextColor={theme.subtleText}
              accessibilityLabel="Email"
              testID="login-email"
              style={[styles.input, { color: theme.text }]}
            />
          </View>
          <Text
            style={[styles.label, { color: theme.mutedText, marginTop: 4 }]}
          >
            Password
          </Text>
          <View
            style={[
              styles.inputWrap,
              { backgroundColor: theme.surface, borderColor: theme.border },
            ]}
          >
            <LockKeyhole size={18} color={theme.subtleText} />
            <TextInput
              value={password}
              onChangeText={setPassword}
              onFocus={clearError}
              secureTextEntry
              textContentType="password"
              placeholder="Password"
              placeholderTextColor={theme.subtleText}
              accessibilityLabel="Password"
              testID="login-password"
              style={[styles.input, { color: theme.text }]}
              onSubmitEditing={() => {
                void submit();
              }}
              returnKeyType="go"
            />
          </View>
        </View>
        <Button
          title="Sign in"
          variant="primary"
          onPress={() => {
            void submit();
          }}
          busy={submitting || booting}
          disabled={!serverUrl.trim() || !email.trim() || !password}
          style={styles.submit}
          testID="login-submit"
        />
        <Text style={[styles.footer, { color: theme.subtleText }]}>
          Your password is sent securely to your server and is never saved on
          this device. Sign-in tokens are stored securely.
        </Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  content: {
    flexGrow: 1,
    justifyContent: "center",
    alignItems: "center",
    paddingHorizontal: 28,
    paddingVertical: 36,
    gap: 16,
  },
  brandMark: {
    width: 60,
    height: 60,
    borderRadius: 21,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 4,
  },
  title: {
    fontSize: 28,
    lineHeight: 36,
    fontWeight: "700",
    textAlign: "center",
  },
  subtitle: {
    maxWidth: 330,
    fontSize: 15,
    lineHeight: 22,
    textAlign: "center",
    marginBottom: 10,
  },
  fields: { width: "100%", gap: 8 },
  label: { fontSize: 13, fontWeight: "600", marginLeft: 3 },
  inputWrap: {
    minHeight: 54,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 15,
    paddingHorizontal: 14,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  input: { flex: 1, paddingVertical: 13, fontSize: 16 },
  submit: { alignSelf: "stretch", marginTop: 5 },
  footer: {
    maxWidth: 320,
    fontSize: 12,
    lineHeight: 18,
    textAlign: "center",
    marginTop: 5,
  },
});
