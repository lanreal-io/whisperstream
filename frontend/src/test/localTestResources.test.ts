import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import os from "node:os";
import { spawnSync } from "node:child_process";
import { localTestResources } from "../../../scripts/local-test-resources";

vi.mock("node:os", () => ({ default: {
  constants: { priority: { PRIORITY_BELOW_NORMAL: 10 } },
  getPriority: vi.fn(() => 0), setPriority: vi.fn(), availableParallelism: vi.fn(() => 12),
} }));
vi.mock("node:child_process", () => {
  const spawnSync = vi.fn();
  return { spawnSync, default: { spawnSync } };
});

describe("local test resource policy", () => {
  beforeEach(() => {
    vi.stubEnv("CI", "");
    vi.stubEnv("WHISPERSTREAM_TEST_LIMITS", "1");
    vi.stubEnv("_WHISPERSTREAM_TEST_CPU_CAPPED", "1");
    // Restore all thread variables modified by the helper after every test.
    for (const key of ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
      "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "RAYON_NUM_THREADS"]) {
      vi.stubEnv(key, process.env[key]);
    }
    vi.mocked(os.getPriority).mockReturnValue(0);
    vi.mocked(os.availableParallelism).mockReturnValue(12);
  });
  afterEach(() => { vi.unstubAllEnvs(); vi.clearAllMocks(); });

  it.each(["true", "1", "TRUE"])("leaves CI=%s untouched", (ci) => {
    vi.stubEnv("CI", ci);
    vi.stubEnv("OMP_NUM_THREADS", "32");
    expect(localTestResources()).toEqual({});
    expect(process.env.OMP_NUM_THREADS).toBe("32");
    expect(os.setPriority).not.toHaveBeenCalled();
    expect(spawnSync).not.toHaveBeenCalled();
  });

  it("supports explicit opt-out", () => {
    vi.stubEnv("WHISPERSTREAM_TEST_LIMITS", "0");
    expect(localTestResources()).toEqual({});
    expect(os.setPriority).not.toHaveBeenCalled();
  });

  it("bounds workers and native threads without compounding an inherited cap", () => {
    vi.stubEnv("OMP_NUM_THREADS", "50");
    vi.stubEnv("MKL_NUM_THREADS", "1");
    vi.stubEnv("OPENBLAS_NUM_THREADS", "bad");
    expect(localTestResources()).toEqual({ maxWorkers: 6, minWorkers: 1 });
    expect(process.env.OMP_NUM_THREADS).toBe("2");
    expect(process.env.MKL_NUM_THREADS).toBe("1");
    expect(process.env.OPENBLAS_NUM_THREADS).toBe("2");
    expect(os.setPriority).toHaveBeenCalledWith(10);
    expect(spawnSync).not.toHaveBeenCalled();
  });

  it.each([[1, 1], [2, 1], [4, 2], [12, 6], [32, 6]])(
    "uses half of %i logical CPUs, at most six: %i workers",
    (cpus, workers) => {
      vi.mocked(os.availableParallelism).mockReturnValue(cpus);
      expect(localTestResources()).toEqual({ maxWorkers: workers, minWorkers: 1 });
    },
  );

  it("preserves an existing lower priority", () => {
    vi.mocked(os.getPriority).mockReturnValue(19);
    localTestResources();
    expect(os.setPriority).not.toHaveBeenCalled();
  });

  it.skipIf(process.platform !== "win32")("warns and retains soft limits if the Windows helper fails", () => {
    vi.stubEnv("_WHISPERSTREAM_TEST_CPU_CAPPED", "");
    vi.mocked(spawnSync).mockReturnValue({ status: 1, stderr: "helper unavailable" } as ReturnType<typeof spawnSync>);
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(localTestResources()).toEqual({ maxWorkers: 6, minWorkers: 1 });
      expect(warning).toHaveBeenCalledWith(expect.stringContaining("CPU cap unavailable"), "helper unavailable");
      expect(process.env._WHISPERSTREAM_TEST_CPU_CAPPED).toBe("");
    } finally { warning.mockRestore(); }
  });
});
