import React, { useState } from "react";
import { ScrollView, StyleSheet, Text } from "react-native";
import { Image } from "expo-image";
import { SvgXml } from "react-native-svg";
import { useTheme } from "../../ui/theme";

/** A zoomable raster image (file or data URI). */
export function ImagePreview({
  uri,
  name,
}: {
  uri: string;
  name: string;
}): React.JSX.Element {
  return (
    <ScrollView
      contentContainerStyle={styles.scroll}
      maximumZoomScale={4}
      minimumZoomScale={1}
      centerContent
    >
      <Image
        source={{ uri }}
        style={styles.image}
        contentFit="contain"
        accessibilityLabel={name}
      />
    </ScrollView>
  );
}

/** An SVG document; invalid markup points to the Source view. */
export function SvgPreview({ xml }: { xml: string }): React.JSX.Element {
  const { theme } = useTheme();
  const [failed, setFailed] = useState(false);
  if (failed)
    return (
      <Text style={{ color: theme.mutedText }}>
        This SVG can't be previewed. Use Source.
      </Text>
    );
  return (
    <SvgXml
      xml={xml}
      width="90%"
      height="70%"
      onError={() => setFailed(true)}
    />
  );
}

const styles = StyleSheet.create({
  scroll: { flexGrow: 1, alignItems: "center", justifyContent: "center" },
  image: { width: "100%", height: 480 },
});
