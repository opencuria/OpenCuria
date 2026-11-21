import React from "react";
import { View } from "react-native";

/** Material keeps one outlined field per row, stacked with a small gap. */
export function FieldGroup({
  children,
}: {
  children: React.ReactNode;
}): React.JSX.Element {
  return <View style={{ gap: 12 }}>{children}</View>;
}
