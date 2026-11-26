import { useEffect, useState } from "react";
import { Image } from "react-native";

/**
 * The pixel size of the image file at `uri` (its own size, not the size an
 * image view decoded it at), or null until known. Recordings point in these
 * pixels, so they match the picture in the workspace.
 */
export function useImageSize(
  uri: string | null | undefined,
): { width: number; height: number } | null {
  const [size, setSize] = useState<{
    uri: string;
    width: number;
    height: number;
  } | null>(null);
  useEffect(() => {
    if (!uri) return;
    let alive = true;
    Image.getSize(
      uri,
      (width, height) => {
        if (alive && width > 0 && height > 0) setSize({ uri, width, height });
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [uri]);
  return size && size.uri === uri
    ? { width: size.width, height: size.height }
    : null;
}
