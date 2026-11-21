import React from "react";
import { StyleSheet, View } from "react-native";
import { useTheme } from "./theme";

/**
 * The text fields of one form as an inset grouped card with hairlines between
 * the rows, as in Settings. Rows are `NativeTextField`s with `grouped`.
 */
export function FieldGroup({
  children,
}: {
  children: React.ReactNode;
}): React.JSX.Element {
  const { theme } = useTheme();
  const rows = React.Children.toArray(children).filter(React.isValidElement);
  return (
    <View style={[styles.card, { backgroundColor: theme.card }]}>
      {rows.map((row, index) => (
        <React.Fragment key={row.key ?? index}>
          {index ? (
            <View
              style={[styles.separator, { backgroundColor: theme.separator }]}
            />
          ) : null}
          <View style={styles.row}>{row}</View>
        </React.Fragment>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderRadius: 22, borderCurve: "continuous", overflow: "hidden" },
  row: { paddingLeft: 18, paddingRight: 16 },
  separator: { height: StyleSheet.hairlineWidth, marginLeft: 18 },
});
