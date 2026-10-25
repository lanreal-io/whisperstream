import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RecordingControlWindow } from "./RecordingControlWindow";

const mocks = vi.hoisted(() => {
  const eventHandlers = new Map<
    string,
    Set<(event: { payload: unknown }) => void>
  >();
  const calls: string[] = [];
  const pendingEmitControl = { deferTrue: false, rejectTrue: false };
  const pendingEmitResolvers: Array<() => void> = [];
  const positionControl = { defer: false };
  const positionResolvers: Array<() => void> = [];
  const currentWindow = {
    setSize: vi.fn(async (size: { width: number; height: number }) => {
      calls.push(`size:${size.width}x${size.height}`);
    }),
    setPosition: vi.fn(async (position: { x: number; y: number }) => {
      calls.push(`position:${position.x},${position.y}`);
    }),
    show: vi.fn(async () => {
      calls.push("tauri-show");
    }),
    hide: vi.fn(async () => {
      calls.push("tauri-hide");
    }),
    outerPosition: vi.fn(async () => ({ x: 918, y: 918 })),
  };
  const hudWindow = {
    isVisible: vi.fn(async () => false),
  };
  const snapGuide = {
    setSize: vi.fn(async () => undefined),
    setPosition: vi.fn(async () => undefined),
    setIgnoreCursorEvents: vi.fn(async () => undefined),
    show: vi.fn(async () => undefined),
    hide: vi.fn(async () => undefined),
  };
  return {
    connected: true,
    socketHandlers: new Set<(event: unknown) => void>(),
    calls,
    emit: vi.fn(async (eventName: string, payload: unknown) => {
      if (
        eventName === "recording-control-start-pending" &&
        payload === true
      ) {
        if (pendingEmitControl.rejectTrue) {
          throw new Error("pending_event_unavailable");
        }
        if (pendingEmitControl.deferTrue) {
          await new Promise<void>((resolve) => {
            pendingEmitResolvers.push(resolve);
          });
        }
      }
    }),
    eventHandlers,
    currentWindow,
    hudWindow,
    snapGuide,
    fetch: vi.fn(),
    getScreenReaderStatus: vi.fn(async () => ({ active: false })),
    invoke: vi.fn(
      async (
        command: string,
        args: Record<string, unknown> = {},
      ) => {
        if (command === "set_hud_window_borderless_visibility") {
          calls.push(args.show ? "borderless-show" : "borderless-hide");
          return;
        }
        if (command === "redraw_current_window") {
          calls.push("redraw");
          return;
        }
        if (command === "set_current_window_logical_position") {
          calls.push(`native-position:${String(args.x)},${String(args.y)}`);
          if (positionControl.defer) {
            await new Promise<void>((resolve) => {
              positionResolvers.push(resolve);
            });
          }
          return;
        }
        throw new Error(`unexpected command: ${command}`);
      },
    ),
    monitor: vi.fn(async () => ({
      scaleFactor: 1,
      position: { x: 0, y: 0 },
      size: { width: 1920, height: 1080 },
      workArea: {
        position: { x: 0, y: 0 },
        size: { width: 1920, height: 1080 },
      },
    })),
    positionControl,
    positionResolvers,
    pendingEmitControl,
    pendingEmitResolvers,
  };
});

vi.mock("../lib/socket/connection", () => ({
    useAppSocket: () => ({
    connected: mocks.connected,
    send: vi.fn(),
    subscribe: (handler: (event: unknown) => void) => {
      mocks.socketHandlers.add(handler);
      return () => mocks.socketHandlers.delete(handler);
    },
  }),
  }));
vi.mock("../lib/socket/agentEvents", () => ({
    sendAgentQuestionOpportunityAnswers: vi.fn(),
    sendAgentQuestionOpportunityDefer: vi.fn(),
    sendAgentResponseDraftEdit: vi.fn(),
    sendAgentResponseDraftSubmit: vi.fn(),
    sendAgentResponseDraftUpdate: vi.fn(),
  }));

vi.mock("../lib/api/client", () => ({
    BASE: "http://localhost:38420",
  }));
vi.mock("../lib/api/dictation", () => ({
    HUD_COMMAND_HEADERS: { "X-Whisperstream-Client": "hud-v1" },
  }));
vi.mock("../lib/recordingControl", () => ({
  BASE: "http://localhost:38420",
  HUD_COMMAND_HEADERS: { "X-Whisperstream-Client": "hud-v1" },
  HUD_MAIN_VISIBILITY_EVENT: "hud-main-visibility-changed",
  RECORDING_CONTROL_HOVER_EVENT: "recording-control-hover",
  RECORDING_CONTROL_START_PENDING_EVENT:
    "recording-control-start-pending",
  RECORDING_CONTROL_VISIBILITY_EVENT:
    "recording-control-visibility-changed",
  RECORDING_CONTROL_VISIBILITY_STORAGE_KEY:
    "ws-recording-control-visible",
  SCREEN_READER_STATUS_EVENT: "screen-reader-status-changed",
  SCREEN_READER_STATUS_STORAGE_KEY: "ws-screen-reader-detected",
  getScreenReaderStatus: vi.fn(async () => ({ active: false })),
  stopSpeech: vi.fn(async () => undefined),
}));
vi.mock("../lib/accessibility", () => ({
  BASE: "http://localhost:38420",
  HUD_COMMAND_HEADERS: { "X-Whisperstream-Client": "hud-v1" },
  HUD_MAIN_VISIBILITY_EVENT: "hud-main-visibility-changed",
  RECORDING_CONTROL_START_PENDING_EVENT:
    "recording-control-start-pending",
  RECORDING_CONTROL_VISIBILITY_EVENT:
    "recording-control-visibility-changed",
  RECORDING_CONTROL_VISIBILITY_STORAGE_KEY:
    "ws-recording-control-visible",
  SCREEN_READER_STATUS_EVENT: "screen-reader-status-changed",
  SCREEN_READER_STATUS_STORAGE_KEY: "ws-screen-reader-detected",
  getScreenReaderStatus: vi.fn(async () => ({ active: false })),
  stopSpeech: vi.fn(async () => undefined),
}));
vi.mock("../lib/api/system", () => ({
    getScreenReaderStatus: mocks.getScreenReaderStatus,
  }));
vi.mock("../lib/api/speech", () => ({
    stopSpeech: vi.fn(async () => undefined),
  }));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: mocks.invoke,
  isTauri: () => false,
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(
    async (
      eventName: string,
      handler: (event: { payload: unknown }) => void,
    ) => {
      const handlers = mocks.eventHandlers.get(eventName) ?? new Set();
      handlers.add(handler);
      mocks.eventHandlers.set(eventName, handlers);
      return () => handlers.delete(handler);
    },
  ),
  emit: mocks.emit,
}));

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => mocks.currentWindow,
  currentMonitor: mocks.monitor,
  Window: {
    getByLabel: vi.fn(async (label: string) =>
      label === "hud" ? mocks.hudWindow : mocks.snapGuide,
    ),
  },
}));

vi.mock("@tauri-apps/api/dpi", () => ({
  LogicalPosition: class LogicalPosition {
    constructor(
      public x: number,
      public y: number,
    ) {}
  },
  LogicalSize: class LogicalSize {
    constructor(
      public width: number,
      public height: number,
    ) {}
  },
}));

vi.mock("framer-motion", async () => {
  const React = await import("react");
  const ignored = new Set([
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
  const motion = new Proxy(
    {},
    {
      get: (_target, tag: string) =>
        ({
          children,
          ...props
        }: Record<string, unknown> & { children?: ReactNode }) => {
          for (const key of ignored) delete props[key];
          return React.createElement(tag, props, children);
        },
    },
  );
  return {
    motion,
    AnimatePresence: ({ children }: { children: ReactNode }) => children,
    useReducedMotion: () => false,
  };
});

function emitTauri(eventName: string, payload: unknown): void {
  act(() => {
    for (const handler of mocks.eventHandlers.get(eventName) ?? []) {
      handler({ payload });
    }
  });
}

async function flushPromises(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 12; index += 1) {
      await Promise.resolve();
    }
  });
}

describe("RecordingControlWindow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.connected = true;
    mocks.socketHandlers.clear();
    mocks.getScreenReaderStatus.mockReset();
    mocks.getScreenReaderStatus.mockResolvedValue({ active: false });
    mocks.calls.length = 0;
    mocks.eventHandlers.clear();
    mocks.fetch.mockReset();
    mocks.fetch.mockResolvedValue({ ok: true, status: 200 });
    mocks.emit.mockClear();
    mocks.pendingEmitControl.deferTrue = false;
    mocks.pendingEmitControl.rejectTrue = false;
    mocks.pendingEmitResolvers.length = 0;
    mocks.invoke.mockClear();
    mocks.positionControl.defer = false;
    mocks.positionResolvers.length = 0;
    mocks.currentWindow.setSize.mockClear();
    mocks.currentWindow.setPosition.mockClear();
    mocks.currentWindow.show.mockClear();
    mocks.currentWindow.hide.mockClear();
    mocks.currentWindow.outerPosition.mockClear();
    mocks.snapGuide.setSize.mockClear();
    mocks.snapGuide.setPosition.mockClear();
    mocks.snapGuide.setIgnoreCursorEvents.mockClear();
    mocks.snapGuide.show.mockClear();
    mocks.snapGuide.hide.mockClear();
    mocks.monitor.mockClear();
    mocks.hudWindow.isVisible.mockReset();
    mocks.hudWindow.isVisible.mockResolvedValue(false);
    localStorage.clear();
    localStorage.setItem("ws-recording-control-visible", "true");
    localStorage.setItem("ws-screen-reader-detected", "false");
    vi.stubGlobal("fetch", mocks.fetch);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("shows only after confirming the main HUD is physically hidden", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();

    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(mocks.hudWindow.isVisible).toHaveBeenCalledTimes(2);
    expect(mocks.calls).toEqual([
      "size:84x40",
      "position:918,918",
      "tauri-show",
    ]);
    expect(mocks.currentWindow.show).toHaveBeenCalledTimes(1);
    expect(
      mocks.invoke.mock.calls.some(
        ([command]) => command === "set_hud_window_borderless_visibility",
      ),
    ).toBe(false);
    expect(
      mocks.invoke.mock.calls.some(
        ([command]) => command === "set_hud_window_region",
      ),
    ).toBe(false);
  });

  it("preserves fresh accessibility state when an enabled preference is repeated", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();
    emitTauri("recording-control-visibility-changed", true);
    await flushPromises();
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    expect(mocks.currentWindow.hide).not.toHaveBeenCalled();
    expect(mocks.getScreenReaderStatus).toHaveBeenCalledOnce();
  });

  it("keeps the replayed native owner blocked until its recovery hide settles", async () => {
    let finish!: () => void;
    mocks.currentWindow.hide.mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(<StrictMode><RecordingControlWindow /></StrictMode>);
    await flushPromises();
    expect(mocks.currentWindow.hide).toHaveBeenCalledOnce();
    expect(mocks.currentWindow.setSize).not.toHaveBeenCalled();
    emitTauri("recording-control-visibility-changed", false);
    emitTauri("recording-control-visibility-changed", true);
    await flushPromises();
    expect(mocks.currentWindow.setSize).not.toHaveBeenCalled();
    await act(async () => { finish(); });
    await flushPromises();
    expect(mocks.currentWindow.show).toHaveBeenCalledOnce();
  });

  it("fails closed when the main HUD becomes visible after the startup read", async () => {
    mocks.hudWindow.isVisible
      .mockReset()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);

    render(<RecordingControlWindow />);
    await flushPromises();

    expect(mocks.hudWindow.isVisible).toHaveBeenCalledTimes(2);
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.currentWindow.setSize).not.toHaveBeenCalled();
  });

  it("keeps press and start pending inside the unchanged launcher vessel", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();
    mocks.calls.length = 0;

    const root = screen.getByTestId("recording-control-window-root");
    fireEvent.pointerEnter(root);
    const start = screen.getByRole("button", { name: "Start dictation" });
    expect(start).toHaveStyle({ background: "transparent" });
    expect(start.querySelectorAll("span")).toHaveLength(0);
    fireEvent.pointerDown(start);
    expect(start.querySelectorAll("span")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();

    expect(screen.getByTestId("recording-control-launcher")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-arming-indicator")).toBeNull();
    expect(mocks.calls).toEqual(["redraw"]);
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
    expect(mocks.fetch).toHaveBeenCalledWith(
      "http://localhost:38420/api/dictation/start",
      {
        method: "POST",
        headers: { "X-Whisperstream-Client": "hud-v1" },
      },
    );
  });

  it("dispatches start pending before posting the backend command", async () => {
    mocks.pendingEmitControl.deferTrue = true;
    render(<RecordingControlWindow />);
    await flushPromises();
    fireEvent.pointerEnter(
      screen.getByTestId("recording-control-window-root"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();

    expect(mocks.emit).toHaveBeenCalledWith(
      "recording-control-start-pending",
      true,
    );
    expect(mocks.fetch).not.toHaveBeenCalled();

    act(() => mocks.pendingEmitResolvers.shift()?.());
    await flushPromises();
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
  });

  it("still posts start when the pending event cannot be dispatched", async () => {
    mocks.pendingEmitControl.rejectTrue = true;
    render(<RecordingControlWindow />);
    await flushPromises();
    fireEvent.pointerEnter(
      screen.getByTestId("recording-control-window-root"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();

    expect(mocks.fetch).toHaveBeenCalledTimes(1);
  });

  it("dispatches pending false when start is rejected", async () => {
    mocks.fetch.mockResolvedValue({ ok: false, status: 409 });
    render(<RecordingControlWindow />);
    await flushPromises();
    fireEvent.pointerEnter(
      screen.getByTestId("recording-control-window-root"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();

    expect(mocks.emit.mock.calls).toEqual([
      ["recording-control-start-pending", true],
      ["recording-control-start-pending", false],
    ]);
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeEnabled();
  });

  it("dispatches pending false after the bounded missed-event timeout", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();
    fireEvent.pointerEnter(
      screen.getByTestId("recording-control-window-root"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flushPromises();
    expect(mocks.emit).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(4999);
      await Promise.resolve();
    });
    expect(mocks.emit).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });
    expect(mocks.emit).toHaveBeenLastCalledWith(
      "recording-control-start-pending",
      false,
    );
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeEnabled();
  });

  it("follows pushed screen-reader status instead of polling", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();
    expect(mocks.getScreenReaderStatus).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("recording-control-seed")).toBeInTheDocument();
    mocks.calls.length = 0;

    act(() => {
      for (const handler of [...mocks.socketHandlers]) {
        handler({ type: "accessibility.screen_reader", active: true });
      }
    });
    await flushPromises();
    expect(mocks.calls).toEqual(["tauri-hide"]);
    expect(localStorage.getItem("ws-screen-reader-detected")).toBe("true");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(mocks.getScreenReaderStatus).toHaveBeenCalledTimes(1);

    mocks.calls.length = 0;
    act(() => {
      for (const handler of [...mocks.socketHandlers]) {
        handler({ type: "accessibility.screen_reader", active: false });
      }
    });
    await flushPromises();
    expect(mocks.calls).toContain("tauri-show");
  });

  it("makes no screen-reader requests while the control preference is off", async () => {
    localStorage.setItem("ws-recording-control-visible", "false");
    render(<RecordingControlWindow />);
    await flushPromises();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(mocks.getScreenReaderStatus).not.toHaveBeenCalled();
    expect(mocks.socketHandlers.size).toBe(0);

    emitTauri("recording-control-visibility-changed", true);
    await flushPromises();
    expect(mocks.getScreenReaderStatus).toHaveBeenCalledTimes(1);
    expect(mocks.socketHandlers.size).toBe(1);

    emitTauri("recording-control-visibility-changed", false);
    await flushPromises();
    expect(mocks.socketHandlers.size).toBe(0);
    expect(mocks.getScreenReaderStatus).toHaveBeenCalledTimes(1);
  });

  it("hides for a main-HUD claim and returns only after its physical hide", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();
    mocks.calls.length = 0;

    emitTauri("hud-main-visibility-changed", true);
    await flushPromises();
    expect(mocks.calls).toEqual(["tauri-hide"]);

    mocks.calls.length = 0;
    emitTauri("hud-main-visibility-changed", false);
    await flushPromises();
    expect(mocks.calls).toEqual([
      "size:84x40",
      "position:918,918",
      "tauri-show",
    ]);
  });

  it("expands and collapses from native hover events", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();

    expect(screen.queryByRole("button", { name: "Start dictation" })).toBeNull();
    emitTauri("recording-control-hover", true);
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeInTheDocument();

    emitTauri("recording-control-hover", false);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.queryByRole("button", { name: "Start dictation" })).toBeNull();
  });

  it("ignores DOM hover once native hover drives the control", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();
    const root = screen.getByTestId("recording-control-window-root");

    emitTauri("recording-control-hover", true);
    emitTauri("recording-control-hover", false);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    fireEvent.pointerEnter(root);
    expect(screen.queryByRole("button", { name: "Start dictation" })).toBeNull();

    emitTauri("recording-control-hover", true);
    fireEvent.pointerLeave(root);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(
      screen.getByRole("button", { name: "Start dictation" }),
    ).toBeInTheDocument();
  });

  it("redraws once after each pointer transition, including the microphone", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();
    mocks.invoke.mockClear();
    mocks.calls.length = 0;

    const root = screen.getByTestId("recording-control-window-root");
    fireEvent.pointerEnter(root);
    fireEvent.pointerMove(root, {
      pointerId: 40,
      screenX: 100,
      screenY: 80,
    });
    fireEvent.pointerLeave(root);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    await flushPromises();
    expect(mocks.invoke).not.toHaveBeenCalled();

    fireEvent.pointerEnter(root);
    const start = screen.getByRole("button", { name: "Start dictation" });
    fireEvent.pointerDown(start, { pointerId: 40 });
    fireEvent.pointerUp(start, { pointerId: 40 });
    fireEvent.pointerCancel(start, { pointerId: 40 });
    await flushPromises();

    expect(mocks.invoke.mock.calls).toEqual([
      ["redraw_current_window"],
      ["redraw_current_window"],
      ["redraw_current_window"],
    ]);
    expect(mocks.calls).toEqual(["redraw", "redraw", "redraw"]);
  });

  it("never captures or performs release bookkeeping for a stationary hold", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();

    const root = screen.getByTestId("recording-control-window-root");
    const setPointerCapture = vi.fn();
    const releasePointerCapture = vi.fn();
    Object.defineProperties(root, {
      setPointerCapture: {
        configurable: true,
        value: setPointerCapture,
      },
      releasePointerCapture: {
        configurable: true,
        value: releasePointerCapture,
      },
    });
    mocks.currentWindow.outerPosition.mockClear();
    mocks.invoke.mockClear();

    fireEvent.pointerDown(root, {
      pointerId: 41,
      screenX: 100,
      screenY: 80,
    });
    await flushPromises();

    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(mocks.snapGuide.show).not.toHaveBeenCalled();
    expect(mocks.currentWindow.outerPosition).toHaveBeenCalledTimes(1);
    expect(mocks.invoke.mock.calls).toEqual([["redraw_current_window"]]);
    mocks.currentWindow.outerPosition.mockClear();

    fireEvent.pointerUp(root, {
      pointerId: 41,
      screenX: 100,
      screenY: 80,
    });
    await flushPromises();

    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(releasePointerCapture).not.toHaveBeenCalled();
    expect(mocks.snapGuide.hide).not.toHaveBeenCalled();
    expect(mocks.currentWindow.outerPosition).not.toHaveBeenCalled();
    expect(localStorage.getItem("hud-position")).toBeNull();
    expect(mocks.invoke.mock.calls).toEqual([
      ["redraw_current_window"],
      ["redraw_current_window"],
    ]);
  });

  it("captures only after threshold and redraws after implicit release", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();

    const root = screen.getByTestId("recording-control-window-root");
    const setPointerCapture = vi.fn();
    const releasePointerCapture = vi.fn();
    Object.defineProperties(root, {
      setPointerCapture: {
        configurable: true,
        value: setPointerCapture,
      },
      releasePointerCapture: {
        configurable: true,
        value: releasePointerCapture,
      },
    });
    mocks.currentWindow.setPosition.mockClear();
    mocks.currentWindow.outerPosition.mockClear();
    mocks.invoke.mockClear();

    fireEvent.pointerDown(root, {
      pointerId: 42,
      screenX: 100,
      screenY: 80,
    });
    await flushPromises();
    fireEvent.pointerMove(root, {
      pointerId: 42,
      screenX: 102,
      screenY: 80,
    });
    await flushPromises();

    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(mocks.snapGuide.show).not.toHaveBeenCalled();

    fireEvent.pointerMove(root, {
      pointerId: 42,
      screenX: 106,
      screenY: 80,
    });
    await flushPromises();

    expect(setPointerCapture).toHaveBeenCalledTimes(1);
    expect(setPointerCapture).toHaveBeenCalledWith(42);
    expect(mocks.snapGuide.show).toHaveBeenCalledTimes(1);
    expect(mocks.currentWindow.setPosition).not.toHaveBeenCalled();
    expect(mocks.invoke).toHaveBeenCalledWith(
      "set_current_window_logical_position",
      { x: 918, y: 918 },
    );

    fireEvent.pointerUp(root, {
      pointerId: 42,
      screenX: 106,
      screenY: 80,
    });
    await flushPromises();

    expect(setPointerCapture).toHaveBeenCalledTimes(1);
    expect(releasePointerCapture).not.toHaveBeenCalled();
    expect(mocks.snapGuide.hide).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("hud-position")).toBe(
      JSON.stringify({ x: 900, y: 920 }),
    );
    expect(
      mocks.invoke.mock.calls.filter(
        ([command]) => command === "redraw_current_window",
      ),
    ).toHaveLength(3);

    fireEvent.lostPointerCapture(root, { pointerId: 42 });
    await flushPromises();

    expect(
      mocks.invoke.mock.calls.filter(
        ([command]) => command === "redraw_current_window",
      ),
    ).toHaveLength(4);
  });

  it("waits for the final synchronous drag move before settled redraw", async () => {
    render(<RecordingControlWindow />);
    await flushPromises();

    const root = screen.getByTestId("recording-control-window-root");
    Object.defineProperty(root, "setPointerCapture", {
      configurable: true,
      value: vi.fn(),
    });
    mocks.currentWindow.setPosition.mockClear();
    mocks.currentWindow.outerPosition.mockClear();
    mocks.invoke.mockClear();

    fireEvent.pointerDown(root, {
      pointerId: 43,
      screenX: 100,
      screenY: 80,
    });
    await flushPromises();
    mocks.currentWindow.outerPosition.mockClear();
    fireEvent.pointerMove(root, {
      pointerId: 43,
      screenX: 106,
      screenY: 80,
    });
    await flushPromises();

    mocks.positionControl.defer = true;
    fireEvent.pointerMove(root, {
      pointerId: 43,
      screenX: 150,
      screenY: 80,
    });
    await flushPromises();

    expect(mocks.currentWindow.setPosition).not.toHaveBeenCalled();
    expect(
      mocks.invoke.mock.calls.filter(
        ([command]) => command === "set_current_window_logical_position",
      ),
    ).toHaveLength(2);
    expect(mocks.positionResolvers).toHaveLength(1);

    fireEvent.pointerUp(root, {
      pointerId: 43,
      screenX: 150,
      screenY: 80,
    });
    fireEvent.lostPointerCapture(root, { pointerId: 43 });
    await flushPromises();

    expect(
      mocks.invoke.mock.calls.filter(
        ([command]) => command === "redraw_current_window",
      ),
    ).toHaveLength(3);
    expect(mocks.currentWindow.outerPosition).not.toHaveBeenCalled();

    act(() => {
      mocks.positionResolvers.shift()?.();
    });
    await flushPromises();

    expect(
      mocks.invoke.mock.calls.filter(
        ([command]) => command === "redraw_current_window",
      ),
    ).toHaveLength(4);
    expect(mocks.currentWindow.outerPosition).toHaveBeenCalledTimes(1);
  });

});
