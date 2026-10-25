import type { AppConfig } from "./api/config";

/** Preserve request order across debounced, immediate and consent saves.
 * A rejected write must not prevent a later corrected draft from saving. */
export function createConfigSaveQueue(save: (patch: Partial<AppConfig>) => Promise<AppConfig>) {
  let tail: Promise<unknown> = Promise.resolve();
  return {
    idle: () => tail,
    save(patch: Partial<AppConfig>): Promise<AppConfig> {
      const operation = tail.then(() => save(patch));
      tail = operation.catch(() => undefined);
      return operation;
    },
  };
}
