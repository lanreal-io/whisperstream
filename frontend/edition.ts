import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";

/**
 * Optional frontend edition. A build that ships extra frontend modules (the
 * official installer's updater) names a JSON manifest in this variable;
 * without it the app builds against the shared stubs. Paths in the manifest
 * are relative to the manifest itself:
 *
 *   updater      module behind the `@edition/updater` alias
 *   windowRoots  window-root modules the split guard keeps out of the entry
 *   allow        extra dev-server file access (directories or files)
 *   tests        extra Vitest globs
 *
 * The manifest sits at the root of the edition's frontend tree. Modules under
 * that directory have no node_modules of their own; their package imports
 * resolve from this app so both share one copy of React and the Tauri APIs.
 */
export const FRONTEND_EDITION_VAR = "WHISPERSTREAM_FRONTEND_EDITION";
export const UPDATER_ALIAS = "@edition/updater";

const APP_ROOT = fileURLToPath(new URL(".", import.meta.url));
// The edition belongs to the tree being built: the repository holding this app.
export const WORKSPACE_ROOT = resolve(APP_ROOT, "..");
const APP_IMPORTER = fileURLToPath(new URL("./src/main.tsx", import.meta.url));
const UPDATER_STUB = fileURLToPath(new URL("./src/lib/officialUpdaterStub.ts", import.meta.url));

export interface FrontendEdition {
  root: string;
  updater: string;
  windowRoots: string[];
  allow: string[];
  tests: string[];
}

interface EditionManifest {
  updater?: unknown;
  windowRoots?: unknown;
  allow?: unknown;
  tests?: unknown;
}

function stringList(value: unknown, field: string, manifest: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${manifest}: "${field}" must be a list of paths`);
  }
  return value as string[];
}

/**
 * The real path of an existing file, refused when it lies outside the
 * repository being built. Containment is checked on real paths, so a link
 * inside the workspace that points at another checkout's provider is refused
 * too: that checkout's modules would otherwise be bundled beside this app's,
 * as a second module graph. Only the manifest and the paths it names are
 * checked; linked node_modules are not involved.
 */
function realInside(path: string, workspace: string, what: string): string {
  if (!existsSync(path)) throw new Error(`${what} does not exist: ${path}`);
  const real = realpathSync.native(path);
  const from = relative(workspace, real);
  if (from.startsWith("..") || isAbsolute(from)) {
    throw new Error(`${FRONTEND_EDITION_VAR}: ${path} is outside the workspace being built (${workspace})`);
  }
  return real;
}

/**
 * The edition named by the environment, or null without one. A variable that
 * names a missing, malformed or out-of-tree manifest is a broken official
 * build, so it fails instead of silently building without the edition.
 */
export function loadFrontendEdition(
  env: NodeJS.ProcessEnv = process.env,
  workspaceRoot: string = WORKSPACE_ROOT,
): FrontendEdition | null {
  const value = env[FRONTEND_EDITION_VAR];
  if (!value) return null;
  const workspace = realpathSync.native(workspaceRoot);
  const manifest = realInside(resolve(value), workspace, `${FRONTEND_EDITION_VAR} manifest`);
  const data = JSON.parse(readFileSync(manifest, "utf8")) as EditionManifest;
  const root = dirname(manifest);
  const at = (path: string) => realInside(resolve(root, path), workspace, `${manifest}: ${path}`);
  if (typeof data.updater !== "string") {
    throw new Error(`${manifest}: "updater" must name the updater module`);
  }
  const app = realpathSync.native(APP_ROOT);
  return {
    root,
    updater: at(data.updater),
    windowRoots: stringList(data.windowRoots, "windowRoots", manifest).map(at),
    allow: stringList(data.allow, "allow", manifest).map(at),
    // Vitest globs are relative to this app, like its own include list.
    tests: stringList(data.tests, "tests", manifest).map((glob) =>
      relative(app, resolve(root, glob)).replace(/\\/g, "/"),
    ),
  };
}

/** The `@edition/updater` alias: the edition's module, or the no-op stub. */
export function editionAliases(edition: FrontendEdition | null): Record<string, string> {
  return { [UPDATER_ALIAS]: edition?.updater ?? UPDATER_STUB };
}

/** Resolves an edition module's package imports from this app. */
export function editionDependencies(edition: FrontendEdition | null): Plugin {
  const root = edition ? resolve(edition.root) : null;
  return {
    name: "edition-dependencies",
    enforce: "pre",
    async resolveId(id, importer, options) {
      if (!root || !importer) return null;
      const from = relative(root, importer);
      if (from.startsWith("..") || isAbsolute(from)) return null;
      if (/^(?:[./\0]|[A-Za-z]:)/.test(id)) return null;
      return this.resolve(id, APP_IMPORTER, { ...options, skipSelf: true });
    },
  };
}
