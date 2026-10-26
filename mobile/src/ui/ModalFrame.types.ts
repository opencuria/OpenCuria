import type React from "react";

export interface ModalFrameProps {
  title: string;
  onClose?: () => void;
  children: React.ReactNode;
}
