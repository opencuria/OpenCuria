import React from "react";
import { Alert } from "react-native";
import * as Application from "expo-application";
import type {
  NativeFormLinkRow,
  NativeFormSection,
} from "../../ui/NativeForm.types";
import { useTheme, type ThemeMode } from "../../ui/theme";
import { useNotifications } from "../../notifications/NotificationContext";
import { avatarColor, profileInitials, profileName } from "../profile";
import { useSettings, useSettingsNavigation } from "../SettingsContext";
import { SettingsPage } from "../SettingsPage";

const NEW_ORGANIZATION = "new-organization";

function link(
  id: string,
  title: string,
  symbol: NativeFormLinkRow["symbol"],
  value?: string,
): NativeFormLinkRow {
  return {
    kind: "link",
    id,
    title,
    symbol,
    route: id,
    value,
    testID: `settings-${id}`,
  };
}

/**
 * The first settings page, laid out like ChatGPT's: the account's avatar and
 * name, the organization card, then grouped pages with large section titles.
 */
export function RootPage(): React.JSX.Element {
  const settings = useSettings();
  const navigation = useSettingsNavigation();
  const { mode, setMode } = useTheme();
  const notifications = useNotifications();
  const { user, organization, organizations, isAdmin } = settings;
  const name = profileName(user);
  const version = [
    Application.nativeApplicationVersion,
    Application.nativeBuildVersion
      ? `(${Application.nativeBuildVersion})`
      : null,
  ]
    .filter(Boolean)
    .join(" ");

  const signOut = () =>
    Alert.alert(
      "Sign out?",
      "Your secure sign-in token will be removed from this device.",
      [
        { text: "Cancel", style: "cancel" },
        { text: "Sign out", style: "destructive", onPress: settings.signOut },
      ],
    );

  const profile: NativeFormSection = {
    id: "profile",
    rows: [
      {
        kind: "profile",
        id: "profile",
        initials: profileInitials(user),
        title: name,
        tint: avatarColor(user),
      },
    ],
  };
  const appearance = {
    kind: "picker",
    id: "appearance",
    label: "Appearance",
    symbol: "circle.lefthalf.filled",
    value: mode,
    groups: [
      {
        options: [
          { value: "system", label: "System" },
          { value: "light", label: "Light" },
          { value: "dark", label: "Dark" },
        ],
      },
    ],
    onChange: (value) => setMode(value as ThemeMode),
  } satisfies NativeFormSection["rows"][number];
  const account: NativeFormSection = {
    id: "account",
    title: "Account",
    prominent: true,
    footer: version ? `OpenCuria ${version}` : undefined,
    rows: [
      {
        kind: "value",
        id: "email",
        label: "Email",
        value: user.email,
        symbol: "envelope",
        stacked: true,
      },
      {
        kind: "value",
        id: "role",
        label: "Role",
        value: isAdmin ? "Admin" : "Member",
        symbol: "person",
      },
      {
        kind: "value",
        id: "server",
        label: "Server",
        value: serverHost(settings.serverUrl),
        symbol: "globe",
      },
      {
        kind: "button",
        id: "sign-out",
        testID: "sign-out",
        title: "Sign out",
        symbol: "rectangle.portrait.and.arrow.right",
        destructive: true,
        onPress: signOut,
      },
    ],
  };

  // Everything else needs the server.
  const sections: NativeFormSection[] = settings.offline
    ? [
        {
          ...profile,
          footer:
            "You’re offline. The other settings return once OpenCuria reconnects.",
        },
        { id: "app", title: "App", prominent: true, rows: [appearance] },
        account,
      ]
    : [
        profile,
        {
          id: "organization",
          rows: [
            {
              kind: "card",
              id: "organization-card",
              testID: "settings-organization",
              title: organization?.name ?? "No organization",
              subtitle: isAdmin
                ? "You manage this organization's settings."
                : "You are a member of this organization.",
              actionTitle: "Switch",
              actions: [
                ...organizations.map((entry) => ({
                  id: entry.id,
                  title: entry.name,
                  selected: entry.id === organization?.id,
                })),
                {
                  id: NEW_ORGANIZATION,
                  title: "New organization",
                  symbol: "plus",
                },
              ],
              onSelect: (id) => {
                if (id === NEW_ORGANIZATION)
                  navigation.push("new-organization");
                else if (id !== organization?.id)
                  settings.selectOrganization(id);
              },
            },
          ],
        },
        {
          id: "configure",
          title: "Configure OpenCuria",
          prominent: true,
          rows: [
            link("providers", "Provider & Models", "cpu"),
            link("agents", "Agents", "sparkles"),
            link("skills", "Skills", "book.closed"),
            link("plugins", "Plugins", "puzzlepiece.extension"),
            link("credentials", "Credentials", "key"),
          ],
        },
        {
          id: "workspaces",
          title: "Workspaces",
          prominent: true,
          rows: [
            link("policy", "Workspace policy", "slider.horizontal.3"),
            link("captured-images", "Captured images", "camera"),
            link(
              "image-definitions",
              "Image definitions",
              "square.stack.3d.up",
            ),
            ...(isAdmin ? [link("runners", "Runners", "server.rack")] : []),
          ],
        },
        {
          id: "access",
          title: "Access",
          prominent: true,
          rows: [
            link("api-keys", "API keys", "key.horizontal"),
            ...(isAdmin
              ? [
                  link(
                    "credential-services",
                    "Credential services",
                    "checkmark.shield",
                  ),
                ]
              : []),
          ],
        },
        {
          id: "app",
          title: "App",
          prominent: true,
          rows: [
            appearance,
            link(
              "notifications",
              "Notifications",
              "bell",
              notifications.state.notifications ? "On" : "Off",
            ),
          ],
        },
        account,
      ];

  return (
    <SettingsPage
      root
      title="Settings"
      sections={sections}
      testID="settings-root"
    />
  );
}

function serverHost(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}
