/**
 * Fail-closed network sandbox for Vitest.
 *
 * The API client defaults to the installed app's port (localhost:38420), so a
 * test that rendered a component without mocking its calls used to drive a
 * running installed app. Every network transport is therefore blocked by
 * default, for every host (which also covers every loopback spelling: 127.x,
 * 0.0.0.0, [::1], localhost with any port, decimal or short IPv4 forms).
 * Blocked requests fail like an unreachable server. Tests that need network
 * behavior must mock it explicitly (`vi.stubGlobal("fetch", ...)`,
 * `vi.stubGlobal("WebSocket", ...)`, or module mocks); a stub replaces the
 * guard and `vi.unstubAllGlobals()` restores it.
 */

const NETWORK_PROTOCOLS = new Set(["http:", "https:", "ws:", "wss:"]);

export const BLOCKED_NETWORK_MESSAGE = "unmocked network request in tests";

function baseHref(): string | undefined {
  try {
    return globalThis.location?.href;
  } catch {
    return undefined;
  }
}

/** True for any URL a test could use to reach a server. Unparseable: blocked. */
export function isBlockedNetworkUrl(input: string | URL): boolean {
  let url: URL;
  try {
    url = new URL(String(input), baseHref());
  } catch {
    return true;
  }
  return NETWORK_PROTOCOLS.has(url.protocol);
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

interface GuardTargets {
  fetch?: typeof fetch;
  XMLHttpRequest?: typeof XMLHttpRequest;
  WebSocket?: typeof WebSocket;
}

/** Install the guard on `target` (globalThis by default). */
export function installNetworkGuard(target: GuardTargets = globalThis): void {
  const realFetch = target.fetch;
  target.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (isBlockedNetworkUrl(requestUrl(input)) || realFetch === undefined) {
      return Promise.reject(
        new TypeError(`Failed to fetch (${BLOCKED_NETWORK_MESSAGE})`),
      );
    }
    return realFetch(input, init);
  }) as typeof fetch;

  const RealXhr = target.XMLHttpRequest;
  if (RealXhr !== undefined) {
    class GuardedXMLHttpRequest extends RealXhr {
      open(method: string, url: string | URL, ...rest: unknown[]): void {
        if (isBlockedNetworkUrl(url)) {
          throw new DOMException(
            `XMLHttpRequest blocked (${BLOCKED_NETWORK_MESSAGE})`,
            "NetworkError",
          );
        }
        (super.open as (...args: unknown[]) => void)(method, url, ...rest);
      }
    }
    target.XMLHttpRequest = GuardedXMLHttpRequest;
  }

  // A blocked socket never connects. It reports error then close on a later
  // task, like an unreachable server, so reconnecting callers back off rather
  // than crash during render.
  class BlockedWebSocket extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;
    readonly CONNECTING = 0;
    readonly OPEN = 1;
    readonly CLOSING = 2;
    readonly CLOSED = 3;
    readonly url: string;
    readonly protocol = "";
    readonly extensions = "";
    readonly bufferedAmount = 0;
    binaryType: BinaryType = "blob";
    readyState = 0;
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    onclose: ((event: CloseEvent | Event) => void) | null = null;

    constructor(url: string | URL) {
      super();
      this.url = String(url);
      setTimeout(() => this.fail(), 0);
    }

    private fail(): void {
      if (this.readyState === 3) return;
      this.readyState = 3;
      const error = new Event("error");
      this.onerror?.(error);
      this.dispatchEvent(error);
      const close = new Event("close");
      this.onclose?.(close);
      this.dispatchEvent(close);
    }

    send(): void {
      throw new DOMException(
        `WebSocket blocked (${BLOCKED_NETWORK_MESSAGE})`,
        "InvalidStateError",
      );
    }

    close(): void {
      this.fail();
    }
  }

  target.WebSocket = BlockedWebSocket as unknown as typeof WebSocket;
}
