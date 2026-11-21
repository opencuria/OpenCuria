import React, { useEffect, useRef, useState } from "react";
import {
  LayoutAnimation,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import {
  KeyboardAvoidingView,
  KeyboardEvents,
  useReanimatedKeyboardAnimation,
} from "react-native-keyboard-controller";
import Animated, {
  FadeInDown,
  interpolate,
  useAnimatedStyle,
  useDerivedValue,
  useSharedValue,
  withSequence,
  withTiming,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ChevronDown, ChevronUp, Globe, Lock, Wifi } from "lucide-react-native";
import { Banner, Button, NativeAction } from "../ui/primitives";
import { FieldGroup } from "../ui/FieldGroup";
import { rejectHaptic } from "../ui/haptics";
import {
  NativeTextField,
  type NativeTextFieldHandle,
} from "../ui/NativeTextField";
import { useTheme } from "../ui/theme";
import { useSession } from "./SessionContext";
import { describeServerUrl, tryNormalizeServerUrl } from "./serverConfig";

const DEFAULT_SERVER_URL = process.env.EXPO_PUBLIC_DEFAULT_SERVER_URL ?? "";
const APP_ICON = require("../../assets/icon.png");

/** The app icon leads; it yields room to the fields while the keyboard is up. */
const ICON_SIZE = 88;
const ICON_SIZE_TYPING = 64;
/** Corner radius per point of size, as on the home screen. */
const ICON_CORNER = 0.225;

/** Hero, fields and action rise in quickly one after another. */
function entering(step: number) {
  return FadeInDown.duration(460).delay(step * 70);
}

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
    shake.value = withSequence(
      withTiming(-10, { duration: 50 }),
      withTiming(9, { duration: 70 }),
      withTiming(-6, { duration: 70 }),
      withTiming(3, { duration: 60 }),
      withTiming(0, { duration: 50 }),
    );
  }, [error, shake]);

  const iconSize = useDerivedValue(() =>
    interpolate(keyboard.progress.value, [0, 1], [ICON_SIZE, ICON_SIZE_TYPING]),
  );
  const iconStyle = useAnimatedStyle(() => ({
    width: iconSize.value,
    height: iconSize.value,
    borderRadius: iconSize.value * ICON_CORNER,
  }));
  const iconCornerStyle = useAnimatedStyle(() => ({
    borderRadius: iconSize.value * ICON_CORNER,
  }));
  const fieldsStyle = useAnimatedStyle(() => ({
    transform: [{ translateX: shake.value }],
  }));
  // Above the home indicator at rest, close above the keyboard while typing.
  const footerStyle = useAnimatedStyle(() => ({
    paddingBottom: interpolate(
      keyboard.progress.value,
      [0, 1],
      [insets.bottom + 16, 16],
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
  const serverLabel = server
    ? describeServerUrl(server.serverUrl)
    : "Set server";
  const ServerIcon = !server ? Globe : local ? Wifi : Lock;
  const Chevron = serverOpen ? ChevronUp : ChevronDown;
  const dark = theme.scheme === "dark";

  return (
    <View
      style={[
        styles.root,
        // Scrolled content ends below the status bar instead of under it.
        { backgroundColor: theme.background, paddingTop: insets.top },
      ]}
    >
      <KeyboardAvoidingView behavior="padding" style={styles.root}>
        <ScrollView
          ref={scroll}
          style={styles.root}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          showsVerticalScrollIndicator={false}
          contentContainerStyle={styles.content}
        >
          <View style={styles.column}>
            <Animated.View entering={entering(0)} style={styles.hero}>
              <Animated.View
                style={[
                  styles.icon,
                  iconStyle,
                  {
                    boxShadow: dark
                      ? "0px 0px 44px rgba(123, 111, 240, 0.32)"
                      : "0px 12px 28px rgba(42, 36, 86, 0.26)",
                  },
                ]}
              >
                <Animated.Image
                  source={APP_ICON}
                  accessible={false}
                  style={[
                    styles.iconImage,
                    iconCornerStyle,
                    // A hairline keeps the dark tile's edge on the black canvas.
                    dark && {
                      borderWidth: StyleSheet.hairlineWidth,
                      borderColor: "rgba(255, 255, 255, 0.16)",
                    },
                  ]}
                />
              </Animated.View>
              <Text
                accessibilityRole="header"
                style={[styles.title, { color: theme.text }]}
              >
                Sign in to OpenCuria
              </Text>
            </Animated.View>

            <Animated.View entering={entering(1)} style={styles.chipFrame}>
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
                style={[styles.chip, { backgroundColor: theme.controlFill }]}
              >
                <ServerIcon
                  size={14}
                  strokeWidth={2.4}
                  color={theme.mutedText}
                />
                <Text
                  numberOfLines={1}
                  style={[styles.chipText, { color: theme.text }]}
                >
                  {serverLabel}
                </Text>
                <Chevron size={15} strokeWidth={2.4} color={theme.subtleText} />
              </NativeAction>
            </Animated.View>

            <Animated.View entering={entering(2)} style={styles.form}>
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
            </Animated.View>
          </View>
        </ScrollView>

        <Animated.View
          entering={entering(3)}
          style={[styles.footer, footerStyle]}
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
  content: {
    flexGrow: 1,
    justifyContent: "center",
    alignItems: "center",
    paddingHorizontal: 24,
    paddingVertical: 24,
  },
  column: { width: "100%", maxWidth: 400 },
  hero: { alignItems: "center", gap: 22 },
  icon: { borderCurve: "continuous" },
  iconImage: {
    width: "100%",
    height: "100%",
    borderCurve: "continuous",
    overflow: "hidden",
  },
  title: {
    fontSize: 28,
    lineHeight: 34,
    fontWeight: "700",
    letterSpacing: -0.5,
    textAlign: "center",
  },
  chipFrame: {
    alignSelf: "center",
    maxWidth: "100%",
    marginTop: 14,
    borderRadius: 17,
    overflow: "hidden",
  },
  chip: {
    alignSelf: "center",
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    height: 34,
    paddingHorizontal: 14,
    borderRadius: 17,
  },
  chipText: { fontSize: 15, fontWeight: "500", flexShrink: 1 },
  form: { marginTop: 36, gap: 12 },
  note: { fontSize: 13, lineHeight: 18, paddingHorizontal: 18 },
  footer: { alignItems: "center", paddingHorizontal: 24, paddingTop: 8 },
  submit: { minHeight: 54 },
});
