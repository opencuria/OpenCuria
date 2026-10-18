import React, { useEffect, useRef, useState } from "react";
import {
  KeyboardAvoidingView,
  LayoutAnimation,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  type TextInputProps,
} from "react-native";
import Svg, { Defs, RadialGradient, Rect, Stop } from "react-native-svg";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  ChevronDown,
  Eye,
  EyeOff,
  Globe,
  LockKeyhole,
  LockOpen,
  Mail,
  Server,
} from "lucide-react-native";
import { Banner, Button, IconButton } from "../ui/primitives";
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
  const emailRef = useRef<TextInput>(null);
  const passwordRef = useRef<TextInput>(null);

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
      <Svg
        pointerEvents="none"
        width="100%"
        height={380}
        style={StyleSheet.absoluteFill}
      >
        <Defs>
          <RadialGradient id="glow" cx="50%" cy="0%" rx="85%" ry="100%">
            <Stop offset="0" stopColor={theme.primary} stopOpacity={0.2} />
            <Stop offset="1" stopColor={theme.primary} stopOpacity={0} />
          </RadialGradient>
        </Defs>
        <Rect width="100%" height="100%" fill="url(#glow)" />
      </Svg>
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
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={`Server ${serverLabel}`}
              accessibilityHint={
                serverOpen ? "Hide server address" : "Change server"
              }
              accessibilityState={{ expanded: serverOpen }}
              onPress={toggleServer}
              hitSlop={8}
              testID="login-server-toggle"
              style={({ pressed }) => [
                styles.serverToggle,
                { opacity: pressed ? 0.6 : 1 },
              ]}
            >
              {insecure ? (
                <LockOpen size={13} color={theme.warning} />
              ) : (
                <Globe size={13} color={theme.subtleText} />
              )}
              <Text
                numberOfLines={1}
                style={[
                  styles.serverToggleText,
                  { color: insecure ? theme.warning : theme.mutedText },
                ]}
              >
                {serverLabel}
              </Text>
              <View
                style={{
                  transform: [{ rotate: serverOpen ? "180deg" : "0deg" }],
                }}
              >
                <ChevronDown size={14} color={theme.subtleText} />
              </View>
            </Pressable>
          </View>

          {error ? (
            <Banner tone="error" message={error} testID="login-error" />
          ) : null}

          <View style={styles.fields}>
            {serverOpen ? (
              <Field
                label="Server"
                icon={<Server size={18} color={theme.subtleText} />}
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
              icon={<Mail size={18} color={theme.subtleText} />}
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
              icon={<LockKeyhole size={18} color={theme.subtleText} />}
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
            textStyle={styles.submitText}
            testID="login-submit"
          />
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

interface FieldProps extends TextInputProps {
  label: string;
  icon: React.ReactNode;
  hint?: string;
  trailing?: React.ReactNode;
  inputRef?: React.Ref<TextInput>;
  testID: string;
}

/** Labelled input row whose border highlights while focused. */
function Field({
  label,
  icon,
  hint,
  trailing,
  inputRef,
  onFocus,
  onBlur,
  style,
  ...input
}: FieldProps): React.JSX.Element {
  const { theme } = useTheme();
  const [focused, setFocused] = useState(false);
  return (
    <View style={styles.field}>
      <Text style={[styles.label, { color: theme.mutedText }]}>{label}</Text>
      <View
        style={[
          styles.inputWrap,
          {
            backgroundColor: focused ? theme.surface : theme.surfaceMuted,
            borderColor: focused ? theme.primary : theme.border,
          },
        ]}
      >
        {icon}
        <TextInput
          ref={inputRef}
          accessibilityLabel={label}
          placeholderTextColor={theme.subtleText}
          selectionColor={theme.primary}
          style={[styles.input, { color: theme.text }, style]}
          onFocus={(event) => {
            setFocused(true);
            onFocus?.(event);
          }}
          onBlur={(event) => {
            setFocused(false);
            onBlur?.(event);
          }}
          {...input}
        />
        {trailing}
      </View>
      {hint ? (
        <Text style={[styles.hint, { color: theme.subtleText }]}>{hint}</Text>
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
  serverToggle: {
    maxWidth: "100%",
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  serverToggleText: { flexShrink: 1, fontSize: 13, lineHeight: 18 },
  fields: { gap: 14 },
  field: { gap: 6 },
  label: { fontSize: 13, fontWeight: "600", marginLeft: 4 },
  inputWrap: {
    minHeight: 54,
    borderWidth: 1,
    borderRadius: 16,
    paddingHorizontal: 14,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  input: { flex: 1, paddingVertical: 13, fontSize: 16 },
  trailing: { marginRight: -10 },
  hint: { fontSize: 12, lineHeight: 17, marginLeft: 4 },
  submit: { alignSelf: "stretch", minHeight: 54, borderRadius: 16 },
  submitText: { fontSize: 16 },
});
