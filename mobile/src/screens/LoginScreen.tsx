import React, { useEffect, useRef, useState } from "react";
import {
  KeyboardAvoidingView,
  LayoutAnimation,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type TextInputProps,
} from "react-native";

import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Eye, EyeOff } from "lucide-react-native";
import { Banner, Button, IconButton } from "../ui/primitives";

import {
  NativeTextField,
  type NativeTextFieldHandle,
} from "../ui/NativeTextField";
import { useTheme } from "../ui/theme";
import { OpenCuriaMark } from "../ui/OpenCuriaMark";
import { useSession } from "./SessionContext";
import { describeServerUrl, tryNormalizeServerUrl } from "./serverConfig";

const DEFAULT_SERVER_URL = process.env.EXPO_PUBLIC_DEFAULT_SERVER_URL ?? "";

export function LoginScreen(): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const { login, error, clearError, booting } = useSession();
  const [serverUrl, setServerUrl] = useState(DEFAULT_SERVER_URL);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const server = tryNormalizeServerUrl(serverUrl);
  // The server only needs attention while it is unset or rejected.
  const [serverOpen, setServerOpen] = useState(!server);
  const emailRef = useRef<NativeTextFieldHandle>(null);
  const passwordRef = useRef<NativeTextFieldHandle>(null);

  useEffect(() => {
    if (error && !server) setServerOpen(true);
  }, [error, server]);

  const toggleServer = () => {
    LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
    setServerOpen((open) => !open);
  };

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

  const insecure = server?.serverUrl.startsWith("http://") ?? false;
  const serverLabel = server
    ? describeServerUrl(server.serverUrl)
    : "Set server";

  return (
    <KeyboardAvoidingView
      style={[styles.root, { backgroundColor: theme.background }]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="on-drag"
        contentContainerStyle={[
          styles.content,
          {
            paddingTop: insets.top + 32,
            paddingBottom: insets.bottom + 32,
          },
        ]}
      >
        <View style={styles.column}>
          <View style={styles.header}>
            <View
              style={[
                styles.brandMark,
                {
                  backgroundColor: theme.surface,
                  borderColor: theme.border,
                  shadowColor: theme.primary,
                },
              ]}
            >
              <OpenCuriaMark size={40} />
            </View>
            <Text
              accessibilityRole="header"
              style={[styles.title, { color: theme.text }]}
            >
              Sign in to OpenCuria
            </Text>
            <Button
              title={serverLabel}
              variant="ghost"
              symbol="server.rack"
              accessibilityLabel={`Server ${serverLabel}`}
              onPress={toggleServer}
              testID="login-server-toggle"
            />
          </View>

          {error ? (
            <Banner tone="error" message={error} testID="login-error" />
          ) : null}

          <View style={styles.fields}>
            {serverOpen ? (
              <Field
                label="Server"
                hint="Use http:// only inside your local network."
                value={serverUrl}
                onChangeText={setServerUrl}
                onFocus={clearError}
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="url"
                placeholder="https://curia.example"
                testID="login-server"
                returnKeyType="next"
                onSubmitEditing={() => emailRef.current?.focus()}
              />
            ) : null}
            <Field
              inputRef={emailRef}
              label="Email"
              value={email}
              onChangeText={setEmail}
              onFocus={clearError}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="email-address"
              autoComplete="email"
              textContentType="username"
              placeholder="you@example.com"
              testID="login-email"
              returnKeyType="next"
              onSubmitEditing={() => passwordRef.current?.focus()}
            />
            <Field
              inputRef={passwordRef}
              label="Password"
              value={password}
              onChangeText={setPassword}
              onFocus={clearError}
              secureTextEntry={!showPassword}
              autoCapitalize="none"
              autoCorrect={false}
              autoComplete="current-password"
              textContentType="password"
              placeholder="Password"
              testID="login-password"
              returnKeyType="go"
              onSubmitEditing={() => {
                void submit();
              }}
              trailing={
                <IconButton
                  symbol={showPassword ? "eye.slash" : "eye"}
                  icon={
                    showPassword ? (
                      <EyeOff size={18} color={theme.subtleText} />
                    ) : (
                      <Eye size={18} color={theme.subtleText} />
                    )
                  }
                  label={showPassword ? "Hide password" : "Show password"}
                  onPress={() => setShowPassword((visible) => !visible)}
                  testID="login-password-toggle"
                  style={styles.trailing}
                />
              }
            />
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
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

interface FieldProps extends Omit<
  TextInputProps,
  "onFocus" | "onSubmitEditing" | "keyboardType" | "returnKeyType"
> {
  onFocus?: () => void;
  onSubmitEditing?: () => void;
  keyboardType?: "email-address" | "url" | "default";
  returnKeyType?: "next" | "go" | "done";
  label: string;
  hint?: string;
  trailing?: React.ReactNode;
  inputRef?: React.Ref<NativeTextFieldHandle>;
  testID: string;
}

/** Platform text fields retain native keyboard, password and accessibility behavior. */
function Field({
  label,
  hint,
  trailing,
  inputRef,
  value = "",
  onChangeText,
  onFocus,
  onSubmitEditing,
  placeholder,
  secureTextEntry,
  keyboardType,
  returnKeyType,
  testID,
}: FieldProps): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={{ gap: 4 }}>
      <NativeTextField
        ref={inputRef}
        label={label}
        value={value}
        onChangeText={(text) => onChangeText?.(text)}
        onFocus={onFocus}
        onSubmitEditing={onSubmitEditing}
        placeholder={placeholder}
        secureTextEntry={secureTextEntry}
        keyboardType={keyboardType}
        returnKeyType={returnKeyType}
        testID={testID}
      />
      {trailing ? (
        <View style={{ alignSelf: "flex-end" }}>{trailing}</View>
      ) : null}
      {hint ? (
        <Text style={{ color: theme.mutedText, fontSize: 12 }}>{hint}</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  content: {
    flexGrow: 1,
    justifyContent: "center",
    alignItems: "center",
    paddingHorizontal: 24,
  },
  column: { width: "100%", maxWidth: 400, gap: 20 },
  header: { alignItems: "center", gap: 10, marginBottom: 6 },
  brandMark: {
    width: 76,
    height: 76,
    borderRadius: 26,
    borderWidth: StyleSheet.hairlineWidth,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 8,
    shadowOffset: { width: 0, height: 10 },
    shadowOpacity: 0.18,
    shadowRadius: 22,
    elevation: 6,
  },
  title: {
    fontSize: 26,
    lineHeight: 32,
    fontWeight: "700",
    letterSpacing: -0.3,
    textAlign: "center",
  },
  fields: { gap: 14 },
  trailing: { marginRight: -10 },
  submit: { alignSelf: "stretch", minHeight: 54, borderRadius: 16 },
  submitText: { fontSize: 16 },
});
