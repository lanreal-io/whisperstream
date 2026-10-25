import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { editionAliases, editionDependencies, loadFrontendEdition } from "./edition";
import { localTestResources, nodeTestCompat } from "../scripts/local-test-resources";

const edition = loadFrontendEdition();

export default defineConfig({
  plugins: [editionDependencies(edition), react()],
  resolve: {
    alias: {
      "@whisperstream/native-ui-test-guest": new URL("./src/lib/nativeUiTestGuest.disabled.ts", import.meta.url).pathname,
      ...editionAliases(edition),
    },
  },
  server: {
    fs: {
      // The dev EULA fixture reads the real selected agreement outside frontend.
      // Vite's raw-transform access check retains the ?raw suffix in its ID.
      // Permit that exact ID rather than exposing the repository root.
      allow: [
        fileURLToPath(new URL(".", import.meta.url)),
        `${fileURLToPath(new URL("../src-tauri/resources/eula/2.1.txt", import.meta.url))}?raw`,
        ...(edition?.allow ?? []),
      ],
    },
  },
  test: {
    ...localTestResources(),
    ...nodeTestCompat(),
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    // An edition's tests run with the app's when the build supplies one.
    include: ["src/**/*.{test,spec}.{ts,tsx}", ...(edition?.tests ?? [])],
    css: false,
  },
});
