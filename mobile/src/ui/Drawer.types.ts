import type React from "react";

export interface DrawerProps {
  visible: boolean;
  onOpen: () => void;
  onClose: () => void;
  title: string;
  navigation: React.ReactNode;
  children: React.ReactNode;
  profileTitle: string;
  onProfile: () => void;
  onWorkspaces: () => void;
  onCreate: () => void;
  canCreate: boolean;
}
