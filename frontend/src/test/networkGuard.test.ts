import { afterEach, describe, expect, it, vi } from "vitest";
import { BLOCKED_NETWORK_MESSAGE, installNetworkGuard, isBlockedNetworkUrl } from "./networkGuard";

const LOOPBACK_SPELLINGS = [
  "http://localhost:38420/api/health",
  "http://LOCALHOST/api",
  "http://localhost./api",
  "https://localhost:1/x",
  "http://127.0.0.1:38420/api",
  "http://127.0.0.2/api",
  "http://127.255.255.254:9/api",
  "http://127.1/api",
  "http://2130706433/api",
  "http://0x7f000001/api",
  "http://0.0.0.0:38420/api",
  "http://[::1]:38420/api",
  "http://[0:0:0:0:0:0:0:1]/api",
  "http://[::ffff:127.0.0.1]/api",
  "ws://localhost:38420/ws",
  "wss://127.0.0.1/ws",
  "ws://[::1]:38420/ws",
  "/api/relative",
  "//localhost:38420/api",
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("test network guard", () => {
  it.each(LOOPBACK_SPELLINGS)("blocks %s", (url) => {
    expect(isBlockedNetworkUrl(url)).toBe(true);
  });

  it("blocks non-loopback hosts too and allows only local data schemes", () => {
    expect(isBlockedNetworkUrl("https://example.com/")).toBe(true);
    expect(isBlockedNetworkUrl("http://192.168.1.10:38420/")).toBe(true);
    expect(isBlockedNetworkUrl("data:text/plain,hi")).toBe(false);
    expect(isBlockedNetworkUrl("blob:http://localhost/abc")).toBe(false);
  });

  it("is installed globally for fetch, XMLHttpRequest and WebSocket", async () => {
    for (const url of ["http://127.0.0.2:38420/api", "http://[::1]/api", "http://0.0.0.0/api"]) {
      await expect(fetch(url)).rejects.toThrow(BLOCKED_NETWORK_MESSAGE);
      await expect(fetch(new Request(url))).rejects.toThrow(BLOCKED_NETWORK_MESSAGE);
      await expect(fetch(new URL(url))).rejects.toThrow(BLOCKED_NETWORK_MESSAGE);
    }

    const xhr = new XMLHttpRequest();
    expect(() => xhr.open("GET", "http://localhost:38420/api/health")).toThrow(
      BLOCKED_NETWORK_MESSAGE,
    );
    expect(() => xhr.open("GET", "http://127.1/api")).toThrow(BLOCKED_NETWORK_MESSAGE);

    vi.useFakeTimers();
    try {
      const socket = new WebSocket("ws://localhost:38420/ws");
      const onerror = vi.fn();
      const onclose = vi.fn();
      const onopen = vi.fn();
      socket.onerror = onerror;
      socket.onclose = onclose;
      socket.onopen = onopen;
      expect(socket.readyState).toBe(WebSocket.CONNECTING);
      expect(() => socket.send("x")).toThrow(BLOCKED_NETWORK_MESSAGE);
      await vi.advanceTimersByTimeAsync(0);
      expect(onerror).toHaveBeenCalledOnce();
      expect(onclose).toHaveBeenCalledOnce();
      expect(onopen).not.toHaveBeenCalled();
      expect(socket.readyState).toBe(WebSocket.CLOSED);
    } finally {
      vi.useRealTimers();
    }
  });

  it("forwards only non-network URLs to the underlying transport", async () => {
    const realFetch = vi.fn(async () => new Response("ok"));
    const target: { fetch?: typeof fetch } = { fetch: realFetch as unknown as typeof fetch };
    installNetworkGuard(target);
    await expect(target.fetch!("http://localhost/x")).rejects.toThrow(BLOCKED_NETWORK_MESSAGE);
    expect(realFetch).not.toHaveBeenCalled();
    await target.fetch!("data:text/plain,hi");
    expect(realFetch).toHaveBeenCalledOnce();
  });

  it("is replaced by explicit stubs and restored afterwards", async () => {
    const stub = vi.fn(async () => new Response("{}"));
    vi.stubGlobal("fetch", stub);
    await fetch("http://localhost:38420/api");
    expect(stub).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
    await expect(fetch("http://localhost:38420/api")).rejects.toThrow(BLOCKED_NETWORK_MESSAGE);
  });
});
