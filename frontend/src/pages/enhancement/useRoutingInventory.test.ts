import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useRoutingInventory } from "./useRoutingInventory";
import type { AppConfig } from "../../lib/api/config";

const icons = vi.hoisted(() => vi.fn());
vi.mock("../../lib/api/apps", () => ({ resolveSiteIcons: icons, resolveApps: vi.fn(), getRunningApps: vi.fn(), getInstalledApps: vi.fn() }));
afterEach(() => { cleanup(); icons.mockReset(); });
const props = (hosts: string[]) => ({ addAppOpen: false, appIdentityEnabled: false, browserEnabled: true, powerModeMap: {}, siteMap: Object.fromEntries(hosts.map((host) => [host, "profile"])) as AppConfig["site_map"] });

it("deduplicates hosts and preserves successful batches when one of 3 batches fails", async () => {
  icons.mockImplementation(async (hosts: string[]) => {
    if (hosts[0] === "site128.example") throw new Error("offline");
    return hosts.map((domain) => ({ domain, icon: "data:image/png;base64,fixture" }));
  });
  const hosts = Array.from({ length: 257 }, (_, i) => `site${String(i).padStart(3, "0")}.example`);
  const { result } = renderHook(() => useRoutingInventory(props([...hosts, hosts[0].toUpperCase()])));
  await act(async () => {});
  expect(icons.mock.calls.map(([hosts]) => hosts.length)).toEqual([128, 128, 1]);
  expect(result.current.siteIcons.size).toBe(129);
});

it("evicts removed keys and ignores a late response owned by an old mapping", async () => {
  let finish!: (rows: unknown[]) => void;
  icons.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  icons.mockResolvedValue([{ domain: "new.example", icon: "new" }]);
  const { result, rerender } = renderHook(({ hosts }) => useRoutingInventory(props(hosts)), { initialProps: { hosts: ["old.example"] } });
  rerender({ hosts: ["new.example"] });
  await act(async () => {});
  await act(async () => { finish([{ domain: "old.example", icon: "old" }]); });
  expect([...result.current.siteIcons]).toEqual([["new.example", "new"]]);
});
