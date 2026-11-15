import { fileURLToPath } from "node:url";
import platform from "./src/platform/test-vite.config";

export default {
  ...platform,
  root: fileURLToPath(new URL(".", import.meta.url)),
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
};
