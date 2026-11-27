import React, { useEffect, useRef, useState } from "react";
import {
  LayoutAnimation,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { StatusBar } from "expo-status-bar";
import {
  KeyboardAvoidingView,
  KeyboardEvents,
  useReanimatedKeyboardAnimation,
} from "react-native-keyboard-controller";
import Animated, {
  interpolate,
  useAnimatedStyle,
  useReducedMotion,
  useSharedValue,
  withSequence,
  withTiming,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ChevronDown, ChevronUp, Lock, Wifi } from "lucide-react-native";
import { Banner, Button, NativeAction } from "../ui/primitives";
import { FieldGroup } from "../ui/FieldGroup";
import { rejectHaptic } from "../ui/haptics";
import {
  NativeTextField,
  type NativeTextFieldHandle,
} from "../ui/NativeTextField";
import { useTheme } from "../ui/theme";
import { LoginStage, STAGE_GROUND } from "./LoginStage";
import { useSession } from "./SessionContext";
import { describeServerUrl, tryNormalizeServerUrl } from "./serverConfig";

const DEFAULT_SERVER_URL = process.env.EXPO_PUBLIC_DEFAULT_SERVER_URL ?? "";

/** The sheet's top corners; it overlaps the stage by as much. */
const SHEET_RADIUS = 32;
/** Room the stage keeps for the mark however much the form needs. */
const STAGE_MIN_HEIGHT = 56;

export function LoginScreen(): React.JSX.Element {
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const reduceMotion = useReducedMotion();
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
  const attempted = useRef(false);
  const scroll = useRef<ScrollView>(null);
  const keyboard = useReanimatedKeyboardAnimation();
  const shake = useSharedValue(0);

  useEffect(() => {
    if (error && !server) setServerOpen(true);
  }, [error, server]);

  // The fields close the content: on short screens the keyboard would cover
  // the last ones, so they scroll fully into view above the action.
  useEffect(() => {
    const shown = KeyboardEvents.addListener("keyboardDidShow", () =>
      scroll.current?.scrollToEnd({ animated: true }),
    );
    return () => shown.remove();
  }, []);

  // A rejected sign-in shakes the fields, like a wrong passcode, and brings
  // its reason into view above the keyboard.
  useEffect(() => {
    if (!error || !attempted.current) return;
    attempted.current = false;
    rejectHaptic();
    requestAnimationFrame(() =>
      scroll.current?.scrollToEnd({ animated: true }),
    );
    if (reduceMotion) return;
    shake.value = withSequence(
      withTiming(-10, { duration: 50 }),
      withTiming(9, { duration: 70 }),
      withTiming(-6, { duration: 70 }),
      withTiming(3, { duration: 60 }),
      withTiming(0, { duration: 50 }),
    );
  }, [error, reduceMotion, shake]);

  const fieldsStyle = useAnimatedStyle(() => ({
    transform: [{ translateX: shake.value }],
  }));
  // Above the home indicator at rest, close above the keyboard while typing.
  const footerStyle = useAnimatedStyle(() => ({
    paddingBottom: interpolate(
      keyboard.progress.value,
      [0, 1],
      [insets.bottom + 16, 12],
    ),
  }));

  const toggleServer = () => {
    LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
    setServerOpen((open) => !open);
  };

  const submit = async () => {
    if (submitting) return;
    setSubmitting(true);
    clearError();
    attempted.current = true;
    try {
      await login(serverUrl, email, password);
    } finally {
      setSubmitting(false);
    }
  };

  const local = server?.serverUrl.startsWith("http://") ?? false;
  const ServerIcon = local ? Wifi : Lock;
  const Chevron = serverOpen ? ChevronUp : ChevronDown;
  const serverLabel = server
    ? describeServerUrl(server.serverUrl)
    : "your server";

  return (
    <View style={[styles.root, { backgroundColor: theme.background }]}>
      {/* The stage stays dark in both appearances. */}
      <StatusBar style="light" />
      <KeyboardAvoidingView behavior="padding" style={styles.root}>
        <ScrollView
          ref={scroll}
          style={styles.root}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          showsVerticalScrollIndicator={false}
          overScrollMode="never"
          contentContainerStyle={styles.content}
        >
          {/* Pulled past the top, the stage continues instead of the canvas. */}
          <View style={styles.stageBleed} />
          <LoginStage
            server={Boolean(server)}
            email={Boolean(email.trim())}
            password={Boolean(password)}
            working={submitting || booting}
            insetTop={insets.top}
            insetBottom={SHEET_RADIUS}
            style={[
              styles.stage,
              { minHeight: insets.top + SHEET_RADIUS + STAGE_MIN_HEIGHT },
            ]}
          />
          <View style={[styles.sheet, { backgroundColor: theme.background }]}>
            <View style={styles.column}>
              <Text
                accessibilityRole="header"
                style={[styles.title, { color: theme.text }]}
              >
                Sign in
              </Text>
              {/* Completes the title: “Sign in to curia.example”. */}
              <NativeAction
                fitContent
                onPress={toggleServer}
                accessibilityLabel={`Server ${serverLabel}`}
                accessibilityHint={
                  serverOpen
                    ? "Hides the server field"
                    : "Shows the server field"
                }
                testID="login-server-toggle"
                style={styles.server}
              >
                <Text style={[styles.serverText, { color: theme.mutedText }]}>
                  to
                </Text>
                {server ? (
                  <ServerIcon
                    size={15}
                    strokeWidth={2.4}
                    color={theme.mutedText}
                  />
                ) : null}
                <Text
                  numberOfLines={1}
                  style={[
                    styles.serverText,
                    styles.host,
                    { color: server ? theme.text : theme.mutedText },
                  ]}
                >
                  {serverLabel}
                </Text>
                <Chevron size={17} strokeWidth={2.4} color={theme.subtleText} />
              </NativeAction>

              <View style={styles.form}>
                <Animated.View style={fieldsStyle}>
                  <FieldGroup>
                    {serverOpen ? (
                      <NativeTextField
                        grouped
                        label="Server"
                        value={serverUrl}
                        onChangeText={setServerUrl}
                        onFocus={clearError}
                        keyboardType="url"
                        placeholder="https://curia.example"
                        testID="login-server"
                        returnKeyType="next"
                        onSubmitEditing={() => emailRef.current?.focus()}
                      />
                    ) : null}
                    <NativeTextField
                      grouped
                      ref={emailRef}
                      label="Email"
                      value={email}
                      onChangeText={setEmail}
                      onFocus={clearError}
                      keyboardType="email-address"
                      placeholder="you@example.com"
                      testID="login-email"
                      returnKeyType="next"
                      onSubmitEditing={() => passwordRef.current?.focus()}
                    />
                    <NativeTextField
                      grouped
                      ref={passwordRef}
                      label="Password"
                      value={password}
                      onChangeText={setPassword}
                      onFocus={clearError}
                      secureTextEntry={!showPassword}
                      testID="login-password"
                      returnKeyType="go"
                      onSubmitEditing={() => {
                        void submit();
                      }}
                      trailingAction={{
                        symbol: showPassword ? "eye.slash" : "eye",
                        label: showPassword ? "Hide password" : "Show password",
                        onPress: () => setShowPassword((visible) => !visible),
                        testID: "login-password-toggle",
                      }}
                    />
                  </FieldGroup>
                </Animated.View>
                {serverOpen ? (
                  <Text style={[styles.note, { color: theme.mutedText }]}>
                    Use http:// only inside your local network.
                  </Text>
                ) : null}
                {error ? (
                  <Banner tone="error" message={error} testID="login-error" />
                ) : null}
              </View>
            </View>
          </View>
        </ScrollView>

        <Animated.View
          style={[
            styles.footer,
            footerStyle,
            { backgroundColor: theme.background },
          ]}
        >
          <View style={styles.column}>
            <Button
              title="Sign in"
              variant="primary"
              shape="capsule"
              onPress={() => {
                void submit();
              }}
              busy={submitting || booting}
              disabled={!serverUrl.trim() || !email.trim() || !password}
              style={styles.submit}
              testID="login-submit"
            />
          </View>
        </Animated.View>
      </KeyboardAvoidingView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  content: { flexGrow: 1 },
  stageBleed: {
    position: "absolute",
    top: -1000,
    height: 1000,
    left: 0,
    right: 0,
    backgroundColor: STAGE_GROUND,
  },
  stage: { flexGrow: 1 },
  sheet: {
    marginTop: -SHEET_RADIUS,
    borderTopLeftRadius: SHEET_RADIUS,
    borderTopRightRadius: SHEET_RADIUS,
    borderCurve: "continuous",
    alignItems: "center",
    paddingTop: 30,
    paddingHorizontal: 24,
    paddingBottom: 8,
  },
  column: { width: "100%", maxWidth: 420 },
  title: {
    fontSize: 40,
    lineHeight: 46,
    fontWeight: "800",
    letterSpacing: -1.2,
  },
  server: {
    alignSelf: "flex-start",
    maxWidth: "100%",
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    minHeight: 44,
  },
  serverText: { fontSize: 19, lineHeight: 24 },
  host: { fontWeight: "600", flexShrink: 1 },
  form: { marginTop: 14, gap: 12 },
  note: { fontSize: 13, lineHeight: 18, paddingHorizontal: 18 },
  footer: { alignItems: "center", paddingHorizontal: 24, paddingTop: 12 },
  submit: { minHeight: 54 },
});
