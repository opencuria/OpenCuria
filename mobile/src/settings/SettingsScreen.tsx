import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Modal, Platform, View } from "react-native";
import type {
  AuthUser,
  OpenCuriaClient,
  Organization,
  Workspace,
} from "@opencuria/client";
import { useTheme } from "../ui/theme";
import { ToastHost } from "../ui/ToastHost";
import { isOrganizationAdmin } from "./profile";
import { ResourceStore } from "./resources";
import { parseRoute } from "./routes";
import {
  SettingsEnvironmentContext,
  SettingsNavigationContext,
  type SettingsChoice,
  type SettingsEnvironment,
  type SettingsNavigation,
} from "./SettingsContext";
import { ChoicePage } from "./pages/ChoicePage";
import { DeletionPage, parseDeletionRoute } from "./pages/DeletionPage";
import { SettingsNavigator } from "./SettingsNavigator";
import { SettingsPage } from "./SettingsPage";
import { RootPage } from "./pages/RootPage";
import { NewOrganizationPage } from "./pages/NewOrganizationPage";
import { NotificationsPage } from "./pages/NotificationsPage";
import { WorkspacePolicyPage } from "./pages/WorkspacePolicyPage";
import { renderAccessRoute } from "./pages/access";
import { renderInfrastructureRoute } from "./pages/infrastructure";
import { renderModelsRoute } from "./pages/models";

export interface SettingsScreenProps {
  visible: boolean;
  onClose: () => void;
  client: OpenCuriaClient;
  user: AuthUser;
  organizations: Organization[];
  organizationId: string | null;
  serverUrl: string;
  workspaces: Workspace[];
  refreshWorkspaces: () => Promise<void>;
  onCatalogChanged: () => void;
  onOpenWorkspace: (workspace: Workspace) => void;
  selectOrganization: (organizationId: string) => void;
  createOrganization: (name: string) => Promise<void>;
  signOut: () => void;
}

const GROUPS = [
  renderModelsRoute,
  renderAccessRoute,
  renderInfrastructureRoute,
];

function renderRoute(
  value: string,
  choices: Map<string, SettingsChoice>,
): React.ReactNode {
  const { page, argument } = parseRoute(value);
  const choice = page === "choice" ? choices.get(argument) : undefined;
  if (choice) return <ChoicePage choice={choice} />;
  switch (page) {
    case "policy":
      return <WorkspacePolicyPage />;
    case "notifications":
      return <NotificationsPage />;
    case "new-organization":
      return <NewOrganizationPage />;
    case "deletion": {
      const deletion = parseDeletionRoute(argument);
      if (deletion) return <DeletionPage {...deletion} />;
      break;
    }
  }
  for (const render of GROUPS) {
    const node = render(page, argument);
    if (node !== undefined) return node;
  }
  return (
    <SettingsPage
      title="Unavailable"
      sections={[]}
      empty={{
        title: "Page unavailable",
        description: "This page no longer exists.",
        symbol: "exclamationmark.triangle.fill",
      }}
    />
  );
}

/**
 * Settings in the style of ChatGPT's: a native page sheet with a navigation
 * stack (SwiftUI on iOS, Material on Android) covering every setting the
 * webapp offers.
 */
export function SettingsScreen(
  props: SettingsScreenProps,
): React.JSX.Element | null {
  const { theme } = useTheme();
  const { visible, onClose, client, organizationId } = props;
  const [path, setPath] = useState<string[]>([]);
  const [sheet, setSheet] = useState<React.ReactNode>(null);
  // Cached pages belong to one opening, account and organization.
  const resources = useMemo(
    () => new ResourceStore(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, organizationId, visible],
  );
  useEffect(() => {
    if (!visible) {
      setPath([]);
      setSheet(null);
    }
  }, [visible]);

  const close = useCallback(() => onClose(), [onClose]);
  // Choice pages keep their options and callback here, keyed by route.
  const choices = useRef(new Map<string, SettingsChoice>());
  const nextChoice = useRef(0);
  const navigation = useMemo<SettingsNavigation>(
    () => ({
      push: (next) =>
        setPath((current) =>
          current[current.length - 1] === next ? current : [...current, next],
        ),
      pop: () => setPath((current) => current.slice(0, -1)),
      replace: (next) => setPath((current) => [...current.slice(0, -1), next]),
      popToRoot: () => setPath([]),
      close,
      choose: (choice) => {
        const id = String(++nextChoice.current);
        choices.current.set(id, choice);
        setPath((current) => [...current, `choice:${id}`]);
      },
      present: setSheet,
    }),
    [close],
  );
  const environment = useMemo<SettingsEnvironment>(
    () => ({
      client,
      user: props.user,
      organizations: props.organizations,
      organizationId,
      organization:
        props.organizations.find((entry) => entry.id === organizationId) ??
        null,
      isAdmin: isOrganizationAdmin(props.organizations, organizationId),
      serverUrl: props.serverUrl,
      resources,
      workspaces: props.workspaces,
      refreshWorkspaces: props.refreshWorkspaces,
      onCatalogChanged: props.onCatalogChanged,
      onOpenWorkspace: props.onOpenWorkspace,
      selectOrganization: props.selectOrganization,
      createOrganization: props.createOrganization,
      signOut: props.signOut,
    }),
    [
      client,
      organizationId,
      resources,
      props.user,
      props.organizations,
      props.serverUrl,
      props.workspaces,
      props.refreshWorkspaces,
      props.onCatalogChanged,
      props.onOpenWorkspace,
      props.selectOrganization,
      props.createOrganization,
      props.signOut,
    ],
  );

  if (!visible) return null;
  return (
    <Modal
      visible
      statusBarTranslucent
      navigationBarTranslucent
      animationType="slide"
      presentationStyle="pageSheet"
      allowSwipeDismissal
      // A swipe down closes the sheet on iOS; Back goes back one page on Android.
      onRequestClose={() =>
        Platform.OS === "android" && path.length ? navigation.pop() : close()
      }
    >
      <SettingsEnvironmentContext.Provider value={environment}>
        <SettingsNavigationContext.Provider value={navigation}>
          <View
            accessibilityViewIsModal
            testID="settings-screen"
            style={{ flex: 1, backgroundColor: theme.background }}
          >
            <SettingsNavigator
              path={path}
              onPathChange={setPath}
              root={<RootPage />}
              renderRoute={(value) => renderRoute(value, choices.current)}
            />
          </View>
          {sheet}
          <ToastHost />
        </SettingsNavigationContext.Provider>
      </SettingsEnvironmentContext.Provider>
    </Modal>
  );
}
