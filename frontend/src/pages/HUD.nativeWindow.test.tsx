/**
 * The HUD's native-window path, with `isTauri()` TRUE.
 *
 * Why this file exists as its own suite: `HUD.test.tsx` mocks
 * `isTauri: () => false`, and several HUD effects are guarded by
 * `if (!isTauri()) return;`. The focusability effect is one of them, so it had
 * never executed in any test — which is exactly how a Tauri call that hides the
 * HUD window shipped twice.
 *
 * The bug this pins: `hudWindow.setFocusable(true)` reaches tao's
 * `WindowFlags::apply_diff`, which runs
 * `if (!new.contains(VISIBLE)) ShowWindow(SW_HIDE)`. Shaped HUD shows use the
 * raw Win32 `ShowWindow` inside `set_hud_window_region`, deliberately bypassing
 * tao, so tao's cached VISIBLE can still be false while the HWND is physically
 * visible. The clean recording handoff repairs that cache, but focusability
 * must remain native for shaped surfaces.
 * Expanding a question was the only path that flipped FOCUSABLE false -> true,
 * so it alone produced a non-empty diff — responses were unaffected, which is
 * precisely the asymmetry the owner reported.
 */

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AppEvent } from "../lib/socket/events";
import { HUD } from "./HUD";
import { NATIVE_MUTATION_STEP_TIMEOUT_MS } from "./hud/nativeMutationChain";

const mocks = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return {
    handlers,
    dedicated: false,
    invoke: vi.fn(
      async (
        _command: string,
        _args?: Record<string, unknown>,
      ): Promise<unknown> => undefined,
    ),
    fetch: vi.fn(),
    listen: vi.fn(async (_event?: string, _handler?: unknown) => () => undefined),
    send: vi.fn(() => true),
    // Every window method the HUD may reach. `setFocusable` is present ON
    // PURPOSE: the assertion is that the HUD never calls it.
    hudWindow: {
      setSize: vi.fn(async () => undefined),
      setPosition: vi.fn(async () => undefined),
      show: vi.fn(async () => undefined),
      hide: vi.fn(async () => undefined),
      setFocus: vi.fn(async () => undefined),
      setFocusable: vi.fn(async () => undefined),
      setIgnoreCursorEvents: vi.fn(async () => undefined),
      outerPosition: vi.fn(async () => ({ x: 0, y: 0 })),
      outerSize: vi.fn(async () => ({ width: 160, height: 80 })),
    },
    currentMonitor: vi.fn(async () => ({
      scaleFactor: 1,
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1080 },
      workArea: {
        position: { x: 0, y: 0 },
        size: { width: 1920, height: 1080 },
      },
    })),
  };
});

vi.mock("../lib/socket/connection", () => ({
    useAppSocket: () => ({
    connected: true,
    send: mocks.send,
    subscribe: (handler: (event: unknown) => void) => {
      mocks.handlers.add(handler);
      return () => mocks.handlers.delete(handler);
    },
  }),
  }));

vi.mock("../lib/api/client", () => ({
    BASE: "http://localhost:38420",
  }));
vi.mock("../lib/recordingControl", () => ({
  BASE: "http://localhost:38420",
  usesDedicatedRecordingControlWindow: () => mocks.dedicated,
  HUD_MAIN_VISIBILITY_EVENT: "hud-main-visibility-changed",
  RECORDING_CONTROL_START_PENDING_EVENT: "recording-control-start-pending",
  RECORDING_CONTROL_VISIBILITY_STORAGE_KEY: "ws-recording-control-visible",
  RECORDING_CONTROL_VISIBILITY_EVENT: "recording-control-visibility-changed",
  SCREEN_READER_STATUS_STORAGE_KEY: "ws-screen-reader-detected",
  SCREEN_READER_STATUS_EVENT: "screen-reader-status-changed",
  HUD_COMMAND_HEADERS: { "X-Whisperstream-Client": "hud-v1" },
  getScreenReaderStatus: async () => ({ ok: true, active: false }),
  stopSpeech: vi.fn(async () => undefined),
}));
vi.mock("../lib/accessibility", () => ({
  BASE: "http://localhost:38420",
  usesDedicatedRecordingControlWindow: () => mocks.dedicated,
  HUD_MAIN_VISIBILITY_EVENT: "hud-main-visibility-changed",
  RECORDING_CONTROL_START_PENDING_EVENT: "recording-control-start-pending",
  RECORDING_CONTROL_VISIBILITY_STORAGE_KEY: "ws-recording-control-visible",
  RECORDING_CONTROL_VISIBILITY_EVENT: "recording-control-visibility-changed",
  SCREEN_READER_STATUS_STORAGE_KEY: "ws-screen-reader-detected",
  SCREEN_READER_STATUS_EVENT: "screen-reader-status-changed",
  HUD_COMMAND_HEADERS: { "X-Whisperstream-Client": "hud-v1" },
  getScreenReaderStatus: async () => ({ ok: true, active: false }),
  stopSpeech: vi.fn(async () => undefined),
}));
vi.mock("../lib/api/dictation", () => ({
    HUD_COMMAND_HEADERS: { "X-Whisperstream-Client": "hud-v1" },
  }));
vi.mock("../lib/api/system", () => ({
    getScreenReaderStatus: async () => ({ ok: true, active: false }),
  }));
vi.mock("../lib/api/speech", () => ({
    stopSpeech: vi.fn(async () => undefined),
  }));

// The whole point of this file.
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => true,
  invoke: mocks.invoke,
}));

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => mocks.hudWindow,
  currentMonitor: mocks.currentMonitor,
  Window: { getByLabel: vi.fn(async () => null) },
}));

vi.mock("@tauri-apps/api/dpi", () => ({
  LogicalPosition: class {
    constructor(
      public x: number,
      public y: number,
    ) {}
  },
  LogicalSize: class {
    constructor(
      public width: number,
      public height: number,
    ) {}
  },
}));

vi.mock("@tauri-apps/api/event", () => ({
  emit: vi.fn(async () => undefined),
  listen: mocks.listen,
}));

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const motionProps = new Set([
    "animate",
    "exit",
    "initial",
    "layout",
    "layoutId",
    "transition",
    "variants",
    "whileHover",
    "whileTap",
  ]);
  // One stable component per tag: a fresh component type on every access
  // would remount the subtree on every render and hide focus bugs.
  const components = new Map<string, (props: Record<string, unknown>) => React.ReactElement>();
  const motion = new Proxy(
    {},
    {
      get: (_target, tag: string) => {
        let component = components.get(tag);
        if (component === undefined) {
          component = ({ children, ...props }: Record<string, unknown>) => {
            for (const key of motionProps) delete props[key];
            return React.createElement(tag, props, children as React.ReactNode);
          };
          components.set(tag, component);
        }
        return component;
      },
    },
  );
  return {
    motion,
    AnimatePresence: ({ children }: { children: React.ReactNode }) => children,
    useReducedMotion: () => true,
  };
});

function emit(event: AppEvent): void {
  act(() => {
    for (const handler of [...mocks.handlers]) handler(event);
  });
}

function lifecycle(
  recordingId: number,
  phase: "recording" | "processing" | "idle",
  overrides: Partial<Extract<AppEvent, { type: "dictation.state" }>> = {},
): Extract<AppEvent, { type: "dictation.state" }> {
  return {
    type: "dictation.state",
    version: 1,
    recording_id: recordingId,
    phase,
    origin: "push_to_talk",
    hud_enabled: true,
    preview_enabled: false,
    screen_reader_detected: false,
    ...overrides,
  };
}

function questionSnapshot(
  revision: number,
  phase = "surfaced",
): Extract<AppEvent, { type: "agent.question.snapshot" }> {
  return {
    type: "agent.question.snapshot",
    version: 1,
    revision,
    active: {
      session_handle: "handle",
      generation: 1,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      phase: phase as any,
      questions: [
        {
          question: "Which environment should I deploy to?",
          header: "Deploy",
          multiSelect: false,
          options: [{ label: "Staging" }, { label: "Production" }],
        },
      ],
      current_index: 0,
      request_id: null,
      routable: false,
      capture_available: false,
      remaining_seconds: 6,
      answers: {},
    },
  };
}

function managedSessionSnapshot(): Extract<AppEvent, { type: "agent.sessions" }> {
  return {
    type: "agent.sessions",
    sessions: [
      {
        session_handle: "managed-one",
        agent: "claude",
        source_name: "whisperstream",
        unread: false,
        needs_input: false,
        can_reply: true,
        latest_response: "Ready",
        conversation_handle: "conversation-one",
        reply_target: "conv:conversation-one",
      },
    ],
  };
}

/** A pending permission ask: the permission pill keeps the shaped native
 *  region owner, so it carries the shaped-family handoff tests. (The compact
 *  question pill is a clean reply surface since the fixed-window change.) */
function permissionSnapshot(
  revision: number,
  sequence = 1,
): Extract<AppEvent, { type: "agent.interactions.snapshot" }> {
  return {
    type: "agent.interactions.snapshot",
    version: 1,
    revision,
    interactions: [
      {
        type: "agent.interaction.state",
        version: 1,
        interaction: {
          request_id: `req-permission-${sequence}`,
          session_handle: "handle",
          kind: "permission",
          sequence,
          status: "pending",
          active: true,
          engaged: true,
          passive_seconds: 6,
          remaining_seconds: 120,
          hard_remaining_seconds: 120,
        },
      },
    ],
  } as unknown as Extract<AppEvent, { type: "agent.interactions.snapshot" }>;
}

function permissionCleared(revision: number): AppEvent {
  return { type: "agent.interactions.snapshot", version: 1, revision, interactions: [] } as unknown as AppEvent;
}

async function flush(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 20; index += 1) await Promise.resolve();
  });
}

describe("HUD native window path (isTauri true)", () => {
  beforeEach(() => {
    mocks.handlers.clear();
    mocks.dedicated = false;
    mocks.invoke.mockReset();
    mocks.invoke.mockImplementation(async (command: string) =>
      command === "set_hud_window_borderless_visibility"
        ? {
            visible: true,
            style: "0x14000000",
            exstyle: "0x8000018",
            nonclient_policy: "use_window_style",
          }
        : undefined,
    );
    for (const method of Object.values(mocks.hudWindow)) method.mockClear();
    mocks.listen.mockReset();
    mocks.listen.mockResolvedValue(() => undefined);
    mocks.fetch.mockReset();
    mocks.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, active: false }),
    });
    vi.stubGlobal("fetch", mocks.fetch);
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    mocks.handlers.clear();
    vi.unstubAllGlobals();
  });

  it("awaits the dedicated sibling's physical hide before applying visible HUD geometry", async () => {
    mocks.dedicated = true;
    let release!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    mocks.invoke.mockImplementation(async command => {
      if (command === "hide_recording_control_window") await pending;
    });
    render(<HUD />);
    await flush();
    mocks.invoke.mockClear();
    emit(questionSnapshot(1));
    await flush();
    expect(mocks.invoke.mock.calls.some(([command]) => command === "hide_recording_control_window")).toBe(true);
    expect(mocks.invoke.mock.calls.some(([command]) => command === "set_hud_bounds")).toBe(false);
    release();
    await flush();
    // The compact question pill is a clean reply surface: its bounds land
    // only once the sibling has physically hidden.
    expect(mocks.invoke.mock.calls.some(([command]) => command === "set_hud_bounds")).toBe(true);
  });

  it("records a landed stale region show before the next clean handoff", async () => {
    let release!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    mocks.invoke.mockImplementation(async (command, args) => {
      if (command === "set_hud_window_region" && args?.show === true) await pending;
    });
    render(<HUD />);
    await flush();
    emit(permissionSnapshot(1));
    await flush();
    expect(mocks.invoke).toHaveBeenCalledWith("set_hud_window_region", expect.objectContaining({ show: true }));
    emit(lifecycle(501, "recording"));
    await flush();
    expect(mocks.invoke).not.toHaveBeenCalledWith("hide_and_clear_hud_window_region", expect.anything());
    release();
    await flush();
    expect(mocks.invoke.mock.calls.some(([command]) => command === "hide_and_clear_hud_window_region")).toBe(true);
  });

  it("does not show superseded geometry after a pending bounds mutation", async () => {
    let release!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    mocks.invoke.mockImplementation(async command => {
      if (command === "set_hud_bounds") await pending;
    });
    render(<HUD />);
    await flush();
    emit(questionSnapshot(1));
    await flush();
    expect(mocks.invoke.mock.calls.some(([command]) => command === "set_hud_bounds")).toBe(true);
    emit(lifecycle(502, "recording"));
    await flush();
    release();
    await flush();
    expect(mocks.invoke.mock.calls.filter(([command, args]) => command === "set_hud_window_region" && args?.show === true)).toHaveLength(0);
    expect(mocks.hudWindow.setSize).toHaveBeenCalled();
  });

  function hangFirst(command: string, predicate: (args?: Record<string, unknown>) => boolean = () => true) {
    let release!: () => void;
    let hung = false;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    mocks.invoke.mockImplementation(async (name, args) => {
      if (name === command && !hung && predicate(args)) {
        hung = true;
        await pending;
      }
      return name === "set_hud_window_borderless_visibility"
        ? { visible: false, style: "0x0", exstyle: "0x0", nonclient_policy: "use_window_style" }
        : undefined;
    });
    return () => release();
  }

  function callIndex(predicate: (command: string, args?: Record<string, unknown>) => boolean): number {
    return mocks.invoke.mock.calls.findIndex(([command, args]) => predicate(command, args));
  }

  async function settleTimers(ms = 0): Promise<void> {
    await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
    await flush();
  }

  it("issues no native work while an invoke hangs, then recovers before applying the successor", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const release = hangFirst("set_hud_bounds");
      render(<HUD />);
      await flush();
      emit(questionSnapshot(1));
      await flush();
      mocks.invoke.mockClear();
      mocks.hudWindow.setSize.mockClear();
      mocks.hudWindow.hide.mockClear();
      emit(lifecycle(504, "recording"));
      await flush();

      await settleTimers(NATIVE_MUTATION_STEP_TIMEOUT_MS * 4);
      // Recovery holds everything: the uncertain call may still land.
      expect(mocks.invoke).not.toHaveBeenCalled();
      expect(mocks.hudWindow.setSize).not.toHaveBeenCalled();
      const timeouts = warn.mock.calls.filter(([line]) =>
        String(line).startsWith("[HUD] native mutation timed out"),
      );
      expect(timeouts).toHaveLength(1);
      expect(String(timeouts[0][0])).toMatch(/^\[HUD\] native mutation timed out gen=\d+ target=\w+ timeout_ms=\d+ recovering=true$/);

      release();
      await flush();
      await settleTimers();
      // The abandoned question step never shows its stale region.
      expect(callIndex((command, args) => command === "set_hud_window_region" && args?.show === true)).toBe(-1);
      // Recovery forces a known hidden state before the successor applies.
      const borderlessHide = callIndex((command, args) => command === "set_hud_window_borderless_visibility" && args?.hide === true);
      const clearRegion = callIndex((command) => command === "hide_and_clear_hud_window_region");
      expect(borderlessHide).toBeGreaterThanOrEqual(0);
      expect(clearRegion).toBeGreaterThan(borderlessHide);
      const lifecycleHide = callIndex((command, args) => command === "apply_window_lifecycle" && args?.action === "hide");
      expect(lifecycleHide).toBeGreaterThan(clearRegion);
      expect(mocks.hudWindow.setSize).toHaveBeenCalled();
      expect(mocks.invoke.mock.invocationCallOrder[lifecycleHide]).toBeLessThan(
        mocks.hudWindow.setSize.mock.invocationCallOrder[0],
      );
    } finally {
      warn.mockRestore();
      vi.useRealTimers();
    }
  });

  it("does not trust a late region show that lands after the successor was requested", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const release = hangFirst("set_hud_window_region", (args) => args?.show === true);
      render(<HUD />);
      await flush();
      emit(permissionSnapshot(1));
      await flush();
      expect(callIndex((command, args) => command === "set_hud_window_region" && args?.show === true)).toBeGreaterThanOrEqual(0);
      emit(lifecycle(505, "recording"));
      await flush();
      await settleTimers(NATIVE_MUTATION_STEP_TIMEOUT_MS);
      mocks.invoke.mockClear();

      // The stale show lands after the successor was requested.
      release();
      await flush();
      await settleTimers();
      // The region the late show left behind is cleared before the clean
      // recording surface is shown; its bookkeeping is not reused.
      const clearRegion = callIndex((command) => command === "hide_and_clear_hud_window_region");
      const lifecycleShow = callIndex((command, args) => command === "apply_window_lifecycle" && args?.action === "show");
      expect(clearRegion).toBeGreaterThanOrEqual(0);
      expect(lifecycleShow).toBeGreaterThan(clearRegion);
      expect(callIndex((command, args) => command === "set_hud_window_region" && args?.show === true)).toBe(-1);
    } finally {
      warn.mockRestore();
      vi.useRealTimers();
    }
  });

  it("reshows the newest shaped surface after a late native hide completes", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(<HUD />);
      await flush();
      emit(permissionSnapshot(1));
      await flush();
      const release = hangFirst("hide_and_clear_hud_window_region");
      emit(permissionCleared(2));
      await flush();
      await settleTimers(301);
      expect(callIndex((command) => command === "hide_and_clear_hud_window_region")).toBeGreaterThanOrEqual(0);
      await settleTimers(NATIVE_MUTATION_STEP_TIMEOUT_MS);
      emit(permissionSnapshot(3, 2));
      await flush();
      mocks.invoke.mockClear();

      release();
      await flush();
      await settleTimers();
      const shows = mocks.invoke.mock.calls.filter(([command, args]) => command === "set_hud_window_region" && args?.show === true);
      expect(shows).toHaveLength(1);
      expect(callIndex((command) => command === "hide_and_clear_hud_window_region")).toBeLessThan(
        callIndex((command, args) => command === "set_hud_window_region" && args?.show === true),
      );
    } finally {
      warn.mockRestore();
      vi.useRealTimers();
    }
  });

  it("keeps one native call outstanding across repeated hangs", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      let outstanding = 0;
      let maxOutstanding = 0;
      const releases: (() => void)[] = [];
      mocks.invoke.mockImplementation(async (command) => {
        outstanding += 1;
        maxOutstanding = Math.max(maxOutstanding, outstanding);
        try {
          if (command === "set_hud_bounds") {
            await new Promise<void>((resolve) => releases.push(resolve));
          }
        } finally {
          outstanding -= 1;
        }
        return command === "set_hud_window_borderless_visibility"
          ? { visible: false, style: "0x0", exstyle: "0x0", nonclient_policy: "use_window_style" }
          : undefined;
      });
      render(<HUD />);
      await flush();
      emit(questionSnapshot(1));
      await flush();
      for (let round = 0; round < 3; round += 1) {
        await settleTimers(NATIVE_MUTATION_STEP_TIMEOUT_MS);
        emit(questionSnapshot(round + 2));
        await flush();
        await settleTimers(NATIVE_MUTATION_STEP_TIMEOUT_MS * 2);
        expect(outstanding).toBe(1);
        expect(releases).toHaveLength(1);
        releases.shift()!();
        await flush();
        await settleTimers();
      }
      expect(maxOutstanding).toBe(1);
      const timeouts = warn.mock.calls.filter(([line]) =>
        String(line).startsWith("[HUD] native mutation timed out"),
      );
      expect(timeouts).toHaveLength(3);
    } finally {
      warn.mockRestore();
      vi.useRealTimers();
    }
  });

  it("shows the newest shaped surface after a superseded pending native hide lands", async () => {
    vi.useFakeTimers();
    try {
      let release!: () => void;
      const pending = new Promise<void>(resolve => { release = resolve; });
      mocks.invoke.mockImplementation(async (command) => {
        if (command === "hide_and_clear_hud_window_region") await pending;
      });
      render(<HUD />);
      await flush();
      emit(permissionSnapshot(1));
      await flush();
      emit(permissionCleared(2));
      await flush();
      await act(async () => { await vi.advanceTimersByTimeAsync(301); });
      expect(mocks.invoke.mock.calls.some(([command]) => command === "hide_and_clear_hud_window_region")).toBe(true);
      emit(permissionSnapshot(3, 2));
      await flush();
      release();
      await flush();
      expect(mocks.invoke.mock.calls.filter(([command, args]) => command === "set_hud_window_region" && args?.show === true)).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the cross-family paint gate closed after native resize rejects", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    const { container } = render(<HUD />);
    await flush();
    mocks.hudWindow.setSize.mockRejectedValueOnce(new Error("native resize failed"));
    emit(lifecycle(503, "recording"));
    await flush();
    expect(container.firstElementChild).toHaveStyle({ visibility: "hidden" });
  });

  it("disposes listener registrations that finish after unmount", async () => {
    const releases: (() => void)[] = [];
    const disposers: { event: string | undefined; dispose: ReturnType<typeof vi.fn> }[] = [];
    mocks.listen.mockImplementation((event) => new Promise(resolve => {
      const dispose = vi.fn();
      disposers.push({ event, dispose });
      releases.push(() => resolve(dispose));
    }));
    const { unmount } = render(<HUD />);
    await flush();
    unmount();
    releases.forEach(release => release());
    await flush();
    expect(disposers.length).toBeGreaterThan(0);
    for (const { event, dispose } of disposers) expect(dispose, event).toHaveBeenCalledOnce();
  });

  it("never routes HUD focusability through Tauri's setFocusable", async () => {
    render(<HUD />);
    await flush();

    emit(questionSnapshot(1));
    await flush();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();

    expect(screen.getByTestId("agent-question-panel")).toBeInTheDocument();
    // tao's apply_diff would hide the window here. It must never be reached.
    expect(mocks.hudWindow.setFocusable).not.toHaveBeenCalled();
  });

  it("makes the expanded question focusable through the native command", async () => {
    render(<HUD />);
    await flush();

    emit(questionSnapshot(1));
    await flush();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();

    expect(mocks.invoke).toHaveBeenCalledWith("set_hud_window_focusable", {
      focusable: true,
    });
  });

  it("opens a managed console as a focused typed draft without Tauri focusability", async () => {
    render(<HUD />);
    await flush();

    emit(managedSessionSnapshot());
    await flush();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();

    // The console opens ready to type; no backend round trip is needed first.
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveFocus();
    expect(mocks.invoke).toHaveBeenCalledWith("set_hud_window_focusable", {
      focusable: true,
    });

    emit(managedSessionSnapshot());
    await flush();

    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveFocus();
    expect(mocks.invoke).toHaveBeenCalledWith("set_hud_window_focusable", {
      focusable: true,
    });
    expect(mocks.hudWindow.setFocusable).not.toHaveBeenCalled();
  });

  it("activates natively on every click into a text field, including an editing one", async () => {
    render(<HUD />);
    await flush();
    emit(managedSessionSnapshot());
    await flush();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();
    const editor = screen.getByRole("textbox", { name: "Edit your reply" });

    mocks.invoke.mockClear();
    fireEvent.pointerUp(editor, { button: 0 });
    await flush();
    fireEvent.pointerUp(editor, { button: 0 });
    await flush();

    // The editing state never changed, so only the press can re-activate.
    const activations = mocks.invoke.mock.calls.filter(
      ([command, args]) =>
        command === "set_hud_window_focusable" &&
        (args as { focusable?: boolean } | undefined)?.focusable === true,
    );
    expect(activations).toHaveLength(2);
    expect(mocks.invoke).toHaveBeenCalledWith("hud_window_foreground_state");
    expect(mocks.hudWindow.setFocusable).not.toHaveBeenCalled();
  });

  it("reports focused only while the HUD is the native foreground window", async () => {
    let foreground = false;
    const base = mocks.invoke.getMockImplementation()!;
    mocks.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) =>
      command === "hud_window_foreground_state" ? foreground : base(command, args),
    );
    render(<HUD />);
    await flush();
    emit(managedSessionSnapshot());
    await flush();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();

    const lastPresented = () =>
      mocks.send.mock.calls
        .map((call) => (call as unknown[])[0] as Record<string, unknown>)
        .filter((message) => message.type === "agent.hud.presented")
        .at(-1);

    // WebView focus without native foreground: the keys would go elsewhere.
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await flush();
    expect(lastPresented()).toMatchObject({ surface: "console", painted: true, focused: false });

    foreground = true;
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await flush();
    expect(lastPresented()).toMatchObject({ surface: "console", painted: true, focused: true });

    act(() => {
      window.dispatchEvent(new Event("blur"));
    });
    await flush();
    expect(lastPresented()).toMatchObject({ focused: false });
  });

  it("gives the field back its caret after activation moves native focus", async () => {
    const pending: Array<() => void> = [];
    const base = mocks.invoke.getMockImplementation()!;
    render(<HUD />);
    await flush();
    emit(managedSessionSnapshot());
    await flush();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();
    const editor = screen.getByRole("textbox", { name: "Edit your reply" }) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "lo" } });
    await flush();
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toBe(editor);
    editor.setSelectionRange(2, 2);
    fireEvent.keyUp(editor, { key: "o" });

    mocks.invoke.mockImplementation((command: string, args?: Record<string, unknown>) =>
      command === "set_hud_window_focusable"
        ? new Promise<unknown>((resolve) => {
            pending.push(() => resolve(undefined));
          })
        : base(command, args),
    );
    fireEvent.pointerUp(editor, { button: 0 });
    // Activation moves native focus to the top-level window and back, which
    // left the WebView caret at the start: the next keys landed before "lo".
    editor.setSelectionRange(0, 0);
    editor.blur();
    await act(async () => {
      for (const release of pending.splice(0)) release();
      await Promise.resolve();
    });
    await flush();

    expect(editor).toHaveFocus();
    expect(editor.selectionStart).toBe(2);
    expect(editor.selectionEnd).toBe(2);
  });

  it("does not re-toggle focusability as the question walks its phases", async () => {
    // A question moves surfaced -> recording -> processing -> draft_ready while
    // it stays open. If any of those fed the focusability effect, each one would
    // tear it down and reapply it, bouncing the foreground to the user's editor
    // and back mid-answer. The desired state changes only with `hudEditing`.
    render(<HUD />);
    await flush();

    emit(questionSnapshot(1));
    await flush();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();
    expect(mocks.invoke).toHaveBeenCalledWith("set_hud_window_focusable", {
      focusable: true,
    });

    mocks.invoke.mockClear();
    for (const [index, phase] of [
      "recording",
      "processing",
      "draft_ready",
    ].entries()) {
      emit(questionSnapshot(index + 2, phase));
      await flush();
    }

    const focusCalls = mocks.invoke.mock.calls.filter(
      ([command]) => command === "set_hud_window_focusable",
    );
    expect(focusCalls).toHaveLength(0);
  });

  it("expands a question by hiding the clean pill once and showing the shaped card", async () => {
    // The 2026-07-29 wedge was an unpaired hide that left the HUD invisible.
    // Expanding now crosses window families (clean pill to shaped card), so
    // it hides deliberately, takes its region while hidden and must show
    // again, never resizing or restyling the visible window.
    render(<HUD />);
    await flush();

    emit(questionSnapshot(1));
    await flush();
    mocks.invoke.mockClear();
    mocks.hudWindow.hide.mockClear();
    emit({ type: "agent-hud-view" } as AppEvent);
    await flush();

    const calls = mocks.invoke.mock.calls.map(([command, args]) => {
      const options = args as { action?: string; show?: boolean; hide?: boolean } | undefined;
      if (command === "apply_window_lifecycle") return `lifecycle:${options?.action}`;
      if (command === "set_hud_window_region") {
        return options?.show ? "region:show" : options?.hide ? "region:hide" : "region";
      }
      return command;
    });
    const hide = calls.indexOf("lifecycle:hide");
    const show = calls.lastIndexOf("region:show");
    expect(hide).toBeGreaterThanOrEqual(0);
    expect(show).toBeGreaterThan(hide);
    expect(calls).not.toContain("region:hide");
    expect(calls.slice(show + 1)).not.toContain("lifecycle:hide");
  });

  it("hands a reply card over to ordinary dictation hidden, then shows the pill once", async () => {
    // Owner report 2026-10-25: a dictation surface interrupting a visible
    // reply card swapped window sizes on screen. The card's window hides,
    // takes the pill's geometry while hidden, then shows.
    render(<HUD />);
    await flush();
    emit(questionSnapshot(1));
    await flush();
    mocks.invoke.mockClear();
    for (const method of Object.values(mocks.hudWindow)) method.mockClear();

    emit(lifecycle(601, "recording"));
    await flush();

    const order = (fn: { mock: { invocationCallOrder: number[] } }) => fn.mock.invocationCallOrder;
    const lifecycleCalls = mocks.invoke.mock.calls
      .map(([command, args], index) => ({ command, args, at: mocks.invoke.mock.invocationCallOrder[index] }))
      .filter(({ command }) => command === "apply_window_lifecycle");
    const hideAt = lifecycleCalls.find(({ args }) => (args as { action: string }).action === "hide")?.at;
    const showAt = lifecycleCalls.find(({ args }) => (args as { action: string }).action === "show")?.at;
    const resizedAt = [...order(mocks.hudWindow.setSize), ...order(mocks.hudWindow.setPosition)];
    expect(hideAt).toBeDefined();
    expect(showAt).toBeDefined();
    expect(resizedAt.length).toBeGreaterThan(0);
    for (const at of resizedAt) {
      expect(at).toBeGreaterThan(hideAt!);
      expect(at).toBeLessThan(showAt!);
    }
  });

  it("keeps the shared-window fallback on one fixed borderless window", async () => {
    localStorage.setItem("ws-recording-control-visible", "true");
    render(<HUD />);
    await flush();

    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(mocks.hudWindow.setSize).toHaveBeenCalledWith({
      width: 132,
      height: 52,
    });
    expect(mocks.invoke).toHaveBeenCalledWith(
      "set_hud_window_borderless_visibility",
      { show: true, hide: false },
    );
    expect(mocks.hudWindow.show).not.toHaveBeenCalled();

    const forbiddenNativeMutations = () =>
      mocks.invoke.mock.calls.filter(([command]) =>
        [
          "set_hud_window_region",
          "set_hud_bounds",
          "hide_and_clear_hud_window_region",
        ].includes(command as string),
      );
    expect(forbiddenNativeMutations()).toHaveLength(0);

    mocks.invoke.mockClear();
    mocks.hudWindow.setSize.mockClear();
    mocks.hudWindow.setPosition.mockClear();
    mocks.hudWindow.show.mockClear();
    mocks.hudWindow.hide.mockClear();

    fireEvent.pointerEnter(screen.getByTestId("hud-root"));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    emit(lifecycle(42, "recording", { origin: "hud" }));
    await flush();
    emit(lifecycle(42, "processing", { origin: "hud" }));
    await flush();
    emit(lifecycle(42, "idle", { origin: "hud" }));
    await flush();

    expect(forbiddenNativeMutations()).toHaveLength(0);
    expect(mocks.invoke).not.toHaveBeenCalledWith(
      "set_hud_window_borderless_visibility",
      expect.anything(),
    );
    expect(mocks.hudWindow.setSize).not.toHaveBeenCalled();
    expect(mocks.hudWindow.setPosition).not.toHaveBeenCalled();
    expect(mocks.hudWindow.show).not.toHaveBeenCalled();
    expect(mocks.hudWindow.hide).not.toHaveBeenCalled();
  });

  // Task 2.1: the stamped 100px vessel lives strictly inside the frozen 120px
  // PILL_WIDTH envelope, so it must move no native bounds, install no region,
  // and re-sequence no show/hide. If this ever fails, the design was violated
  // by a setBounds/region call rather than by a painted width.
  it("issues no native geometry for the stamped recording vessel", async () => {
    render(<HUD />);
    await flush();

    emit(lifecycle(77, "recording"));
    await flush();
    mocks.invoke.mockClear();
    mocks.hudWindow.setSize.mockClear();
    mocks.hudWindow.setPosition.mockClear();
    mocks.hudWindow.show.mockClear();
    mocks.hudWindow.hide.mockClear();

    emit({
      type: "enhancement.profile",
      recording_id: 77,
      profile_id: "chat",
      name: "Chat",
    } as AppEvent);
    await flush();
    expect(screen.getByTestId("hud-profile-mark")).toBeInTheDocument();

    emit(lifecycle(77, "processing"));
    await flush();

    expect(screen.getByTestId("hud-profile-mark")).toBeInTheDocument();
    expect(
      mocks.invoke.mock.calls.filter(([command]) =>
        [
          "set_hud_window_region",
          "set_hud_bounds",
          "hide_and_clear_hud_window_region",
        ].includes(command as string),
      ),
    ).toHaveLength(0);
    expect(mocks.hudWindow.setSize).not.toHaveBeenCalled();
    expect(mocks.hudWindow.setPosition).not.toHaveBeenCalled();
    expect(mocks.hudWindow.show).not.toHaveBeenCalled();
    expect(mocks.hudWindow.hide).not.toHaveBeenCalled();
  });

  it("exports a native borderless invariant failure through the frontend log", async () => {
    const logError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.invoke.mockImplementation(async (command: string) => {
      if (command === "set_hud_window_borderless_visibility") {
        throw new Error(
          "HUD borderless invariant failed after native show visible=true style=0x14CB0000",
        );
      }
    });
    localStorage.setItem("ws-recording-control-visible", "true");

    render(<HUD />);
    await flush();

    expect(logError).toHaveBeenCalledWith(
      expect.stringContaining(
        "[HUD] borderless visibility failed gen=",
      ),
    );
    expect(logError).toHaveBeenCalledWith(
      expect.stringContaining("style=0x14CB0000"),
    );
    expect(mocks.hudWindow.show).not.toHaveBeenCalled();
    logError.mockRestore();
  });

  it("exports the sanitized native style snapshot through the frontend log", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    localStorage.setItem("ws-recording-control-visible", "true");

    render(<HUD />);
    await flush();

    expect(log).toHaveBeenCalledWith(
      expect.stringContaining(
        "[HUD] borderless visibility applied gen=",
      ),
    );
    expect(log).toHaveBeenCalledWith(
      expect.stringContaining(
        "visible=true style=0x14000000 exstyle=0x8000018 nonclient_policy=use_window_style",
      ),
    );
    log.mockRestore();
  });
});
