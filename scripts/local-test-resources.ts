import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

// Config-time setup covers npm test and direct npx vitest, before worker creation.
export function localTestResources() {
  if (["1", "true"].includes((process.env.CI ?? "").toLowerCase()) ||
      process.env.WHISPERSTREAM_TEST_LIMITS === "0") return {};

  for (const name of ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "RAYON_NUM_THREADS"]) {
    const value = Number(process.env[name]);
    process.env[name] = String(Number.isInteger(value) && value > 0 ? Math.min(value, 2) : 2);
  }
  try {
    const priority = os.constants.priority.PRIORITY_BELOW_NORMAL;
    if (os.getPriority() < priority) os.setPriority(priority);
  } catch (error) {
    console.warn("[tests] Could not lower process priority:", error);
  }
  if (process.platform === "win32" && process.env._WHISPERSTREAM_TEST_CPU_CAPPED !== "1") {
    const helper = path.join(path.dirname(fileURLToPath(import.meta.url)), "local_test_resources.py");
    // uv runs only the stdlib helper, without syncing the backend environment.
    const result = spawnSync("uv", ["run", "--no-project", "--offline", "python", helper,
      "--attach", String(process.pid)], { encoding: "utf8", windowsHide: true, timeout: 15000 });
    if (result.status === 0) {
      process.env._WHISPERSTREAM_TEST_CPU_CAPPED = "1";
      console.info("[tests] Windows process-tree CPU cap active.");
      if (result.stderr) console.warn(result.stderr.trim());
    } else {
      console.warn("[tests] CPU cap unavailable; retaining worker/thread/priority limits.",
        result.error?.message ?? result.stderr.trim());
    }
  }
  // Half the logical CPUs, at most six; mirrors worker_count() in local_test_resources.py.
  return { maxWorkers: Math.max(1, Math.min(6, Math.floor(os.availableParallelism() / 2))), minWorkers: 1 };
}

// Node 25 enables built-in Web Storage by default. Its global localStorage
// shadows jsdom's, so storage-backed tests fail. Turn it off in Vitest workers
// wherever the flag exists (Node 22.4+); older Node never had the conflict.
// Applies in CI too, unlike the resource limits above.
export function nodeTestCompat() {
  if (!process.allowedNodeEnvironmentFlags.has("--experimental-webstorage")) return {};
  const execArgv = ["--no-experimental-webstorage"];
  return { poolOptions: { forks: { execArgv }, threads: { execArgv }, vmForks: { execArgv }, vmThreads: { execArgv } } };
}
