import React, { useEffect, useRef, useState, type ComponentType } from "react";
import {
  ActivityIndicator,
  Animated,
  Platform,
  StyleSheet,
  Text,
  View,
  type NativeSyntheticEvent,
  type ViewProps,
} from "react-native";
import { requireNativeView } from "expo";
import { useTheme } from "../../ui/theme";

const NATIVE_PDF_VIEW = "OpenCuriaPdf";

interface NativePdfViewProps extends ViewProps {
  source: string;
  /** Android draws edge to edge; iOS PDFKit follows the safe area itself. */
  contentInsetBottom?: number;
  onLoad?: (event: NativeSyntheticEvent<{ pageCount: number }>) => void;
  onError?: (
    event: NativeSyntheticEvent<{ message: string; code: string }>,
  ) => void;
  onPageChange?: (
    event: NativeSyntheticEvent<{ page: number; pageCount: number }>,
  ) => void;
}

let cachedView: ComponentType<NativePdfViewProps> | undefined;

/** PDFKit / PdfRenderer view, or null in development clients built before it. */
function nativePdfView(): ComponentType<NativePdfViewProps> | null {
  if (cachedView) return cachedView;
  const expo = (
    globalThis as {
      expo?: { getViewConfig?: (name: string) => unknown };
    }
  ).expo;
  // Both lookups throw on runtimes without native views (web, tests).
  try {
    if (expo?.getViewConfig?.(NATIVE_PDF_VIEW) == null) return null;
    cachedView = requireNativeView<NativePdfViewProps>(NATIVE_PDF_VIEW);
  } catch {
    return null;
  }
  return cachedView;
}

export function hasNativePdfView(): boolean {
  return nativePdfView() !== null;
}

/**
 * A local PDF in the platform's document view: continuous pages with pinch
 * and double-tap zoom. A page counter appears briefly while paging.
 */
export function PdfPreview({
  uri,
  name,
  bottomInset,
  onError,
}: {
  uri: string;
  name: string;
  bottomInset: number;
  onError: (message: string) => void;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const NativePdfView = nativePdfView();
  const [loaded, setLoaded] = useState(false);
  const [page, setPage] = useState<{ page: number; count: number } | null>(
    null,
  );
  const counter = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (!page || page.count < 2) return;
    counter.stopAnimation();
    counter.setValue(1);
    const fade = Animated.timing(counter, {
      toValue: 0,
      delay: 1200,
      duration: 300,
      useNativeDriver: true,
    });
    fade.start();
    return () => fade.stop();
  }, [counter, page]);

  if (!NativePdfView) return null;
  return (
    <View style={[styles.fill, { backgroundColor: theme.surfaceMuted }]}>
      <NativePdfView
        testID="file-pdf"
        accessibilityLabel={name}
        source={uri}
        contentInsetBottom={Platform.OS === "android" ? bottomInset : undefined}
        style={StyleSheet.absoluteFill}
        onLoad={() => setLoaded(true)}
        onError={(event) => onError(event.nativeEvent.message)}
        onPageChange={(event) =>
          setPage({
            page: event.nativeEvent.page,
            count: event.nativeEvent.pageCount,
          })
        }
      />
      {!loaded ? (
        <View style={styles.center} pointerEvents="none">
          <ActivityIndicator color={theme.subtleText} />
        </View>
      ) : null}
      {page && page.count > 1 ? (
        <Animated.View
          pointerEvents="none"
          style={[styles.counter, { opacity: counter }]}
        >
          <Text style={styles.counterText} accessibilityLiveRegion="polite">
            {page.page} of {page.count}
          </Text>
        </Animated.View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  center: {
    ...StyleSheet.absoluteFill,
    alignItems: "center",
    justifyContent: "center",
  },
  // Readable above white pages in either appearance, like Quick Look's.
  counter: {
    position: "absolute",
    top: 12,
    left: 12,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 999,
    backgroundColor: "rgba(0, 0, 0, 0.6)",
  },
  counterText: {
    color: "#FFFFFF",
    fontSize: 13,
    fontWeight: "500",
    fontVariant: ["tabular-nums"],
  },
});
