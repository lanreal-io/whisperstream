import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, searchForWorkspaceRoot, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { editionAliases, editionDependencies, loadFrontendEdition } from "./edition";
import {
  WINDOW_ROOT_MODULE_SUFFIXES,
  findUnregisteredStaticWindowPaths,
} from "./src/windowRoots";

const WINDOW_ROOT_MODULES = Object.values(WINDOW_ROOT_MODULE_SUFFIXES);

const DEV_ONLY_MODULES = [
  "/src/pages/FixtureTest.tsx",
  "/src/pages/UiTest.tsx",
  "/src/pages/DialogShowcase.tsx",
  "/src/dev/dialogRegistry.tsx",
  "/src/pages/hud/HudAgentMarkComparison.tsx",
  "/src/dev/demo/DemoStage.tsx",
] as const;

interface TauriWindowConfig {
  url?: string;
}

interface TauriConfig {
  app?: {
    windows?: TauriWindowConfig[];
  };
}

function getUnregisteredStaticWindowPaths(): string[] {
  const config = JSON.parse(
    readFileSync(
      new URL("../src-tauri/tauri.conf.json", import.meta.url),
      "utf8",
    ),
  ) as TauriConfig;
  return findUnregisteredStaticWindowPaths(
    (config.app?.windows ?? []).map(
      (windowConfig) => windowConfig.url ?? "/",
    ),
  );
}

function windowRootSplitGuard(editionWindowRoots: readonly string[]): Plugin {
  // Edition window roots are absolute paths; shared ones are path suffixes.
  const windowRootModules = [
    ...WINDOW_ROOT_MODULES,
    ...editionWindowRoots.map((path) => path.replaceAll("\\", "/")),
  ];
  return {
    name: "window-root-split-guard",
    apply: "build",
    buildStart() {
      const unregisteredWindowPaths = getUnregisteredStaticWindowPaths();
      if (unregisteredWindowPaths.length > 0) {
        this.error(
          `Startup-static Tauri window paths lack explicit dynamic roots: ${unregisteredWindowPaths.join(", ")}. ` +
            "Register each path and loader in windowRoots.ts and main.tsx.",
        );
      }
    },

    generateBundle(_options, bundle) {
      const chunks = Object.values(bundle).filter(
        (output) => output.type === "chunk",
      );
      const entry = chunks.find((chunk) => chunk.isEntry);
      if (!entry) {
        this.error("Window root split guard could not find the HTML entry chunk.");
      }

      const entryModules = Object.keys(entry.modules).map((id) =>
        id.replaceAll("\\", "/"),
      );
      const leakedWindowRoot = windowRootModules.find((suffix) =>
        entryModules.some((id) => id.endsWith(suffix)),
      );
      if (leakedWindowRoot) {
        this.error(
          `Window root ${leakedWindowRoot} was statically included in the shared startup entry.`,
        );
      }

      const productionModules = chunks.flatMap((chunk) =>
        Object.keys(chunk.modules).map((id) => id.replaceAll("\\", "/")),
      );
      const leakedDevModule = DEV_ONLY_MODULES.find((suffix) =>
        productionModules.some((id) => id.endsWith(suffix)),
      );
      if (leakedDevModule) {
        this.error(`Dev-only module ${leakedDevModule} was emitted in production.`);
      }
    },
  };
}

export default defineConfig(() => {
  const nativeUiTestEnabled = process.env.VITE_NATIVE_UI_TEST === "1";
  const disabledGuest = fileURLToPath(
    new URL("./src/lib/nativeUiTestGuest.disabled.ts", import.meta.url),
  );
  const nativeUiTestGuest = nativeUiTestEnabled
    ? process.env.VITE_NATIVE_UI_TEST_GUEST
    : disabledGuest;
  if (!nativeUiTestGuest) {
    throw new Error(
      "VITE_NATIVE_UI_TEST_GUEST is required when VITE_NATIVE_UI_TEST=1",
    );
  }

  const edition = loadFrontendEdition();

  return {
    plugins: [
      editionDependencies(edition),
      react(),
      tailwindcss(),
      windowRootSplitGuard(edition?.windowRoots ?? []),
    ],
    resolve: {
      alias: {
        "@whisperstream/native-ui-test-guest": nativeUiTestGuest,
        ...editionAliases(edition),
      },
    },
    clearScreen: false,
    server: {
      port: 1420,
      strictPort: true,
      fs: { allow: [searchForWorkspaceRoot(process.cwd()), ...(edition?.allow ?? [])] },
    },
    build: {
      outDir: nativeUiTestEnabled ? "dist-native-ui-test" : "dist",
      target: "esnext",
    },
  };
});
