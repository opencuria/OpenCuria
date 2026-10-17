import { fileURLToPath } from "node:url";

export default {
  resolve: {
    alias: {
      "@opencuria/chat-core": fileURLToPath(
        new URL("../../../packages/chat-core/src", import.meta.url),
      ),
      "@opencuria/client": fileURLToPath(
        new URL("../../../packages/client/src/index.ts", import.meta.url),
      ),
      "expo-file-system": fileURLToPath(
        new URL("./test-mocks/expo-file-system.ts", import.meta.url),
      ),
      "expo-sharing": fileURLToPath(
        new URL("./test-mocks/expo-sharing.ts", import.meta.url),
      ),
      "expo-secure-store": fileURLToPath(
        new URL("./test-mocks/expo-secure-store.ts", import.meta.url),
      ),
      "expo-document-picker": fileURLToPath(
        new URL("./test-mocks/expo-document-picker.ts", import.meta.url),
      ),
      "expo-image-picker": fileURLToPath(
        new URL("./test-mocks/expo-image-picker.ts", import.meta.url),
      ),
      "expo-image-manipulator": fileURLToPath(
        new URL("./test-mocks/expo-image-manipulator.ts", import.meta.url),
      ),
    },
  },
  test: { environment: "node", include: ["**/*.test.ts"] },
};
