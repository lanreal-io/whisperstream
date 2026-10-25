/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Set to "1" by `scripts/build.py --experimental` (a personal dogfood build)
   * so the agent-speech experimental gate stays live in the bundle. Unset in
   * dev and production, so `import.meta.env.VITE_EXPERIMENTAL === "1"` is
   * statically false in a release build.
   */
  readonly VITE_EXPERIMENTAL?: string;
}
