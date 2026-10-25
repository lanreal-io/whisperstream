import { afterEach, expect, it, vi } from "vitest";
import { startDownload, cancelDownload } from "./api/models";
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
it("waits through startup model-lock contention then starts the selected download", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({reason:"operation_busy"}), {status:409}))
    .mockResolvedValueOnce(new Response(JSON.stringify({status:"started"})));
  vi.stubGlobal("fetch", fetch);
  const result = startDownload();
  await vi.advanceTimersByTimeAsync(500);
  await result;
  expect(fetch).toHaveBeenCalledTimes(2);
});
it("does not retry dictation conflicts", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({reason:"dictation_active"}), {status:409}));
  vi.stubGlobal("fetch", fetch);
  await expect(startDownload()).rejects.toThrow("409");
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("cancel prevents a pending start from restarting the download", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({reason:"operation_busy"}), {status:409}))
    .mockResolvedValueOnce(new Response(JSON.stringify({status:"not_running"})));
  vi.stubGlobal("fetch", fetch);
  const result = expect(startDownload()).rejects.toMatchObject({name:"AbortError"});
  await vi.advanceTimersByTimeAsync(0);
  await cancelDownload();
  await vi.advanceTimersByTimeAsync(500);
  await result;
  expect(fetch).toHaveBeenCalledTimes(2);
});
