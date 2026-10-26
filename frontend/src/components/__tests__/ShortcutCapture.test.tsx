/**
 * Vitest + Testing Library coverage for ShortcutCapture (hotkey-cancel iteration 01).
 *
 * Tests cover:
 *   - Resting controls never show persistent clear chrome.
 *   - Capturing an existing binding reveals the integrated clear action.
 *   - Clear exits capture and unbinds; click-again and Escape only cancel.
 *   - Empty and non-clearable captures never show the clear action.
 *   - A completed capture replaces the existing value.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act } from "@testing-library/react";
import { ShortcutCapture } from "../ShortcutCapture";

// framer-motion must be mocked so AnimatePresence / motion.span don't need a
// browser animation engine in jsdom. Mirrors ProfileEditorModal.test.tsx.
vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...p }: React.HTMLAttributes<HTMLDivElement>) => (
      <div {...p}>{children}</div>
    ),
    span: ({ children, ...p }: React.HTMLAttributes<HTMLSpanElement>) => (
      <span {...p}>{children}</span>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeProps(
  overrides: Partial<Parameters<typeof ShortcutCapture>[0]> = {},
) {
  return {
    value: "",
    onChange: vi.fn(),
    send: vi.fn(() => true),
    subscribe: vi.fn(() => () => {}),
    connected: true,
    ...overrides,
  };
}

function captureEvents() {
  type Handler = Parameters<Parameters<typeof ShortcutCapture>[0]["subscribe"]>[0];
  let handler: Handler | undefined;
  const subscribe = vi.fn((nextHandler: Handler) => {
    handler = nextHandler;
    return () => {};
  });
  return {
    subscribe,
    emit: (event: Parameters<Handler>[0]) => handler?.(event),
  };
}

// ---------------------------------------------------------------------------
// Entry-only clear action
// ---------------------------------------------------------------------------

describe("ShortcutCapture — entry-only clear action", () => {
  it("leaves capture without changing the binding when the native listener fails", () => {
    const events = captureEvents();
    const p = makeProps({ value: "shift_r", subscribe: events.subscribe });
    render(<ShortcutCapture {...p} />);
    fireEvent.click(screen.getByRole("button"));
    act(() => events.emit({ type: "shortcut.capture.failed", target: "primary" }));
    expect(screen.queryByText("Press shortcut")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Your shortcut isn't ready. Try again.");
    expect(p.onChange).not.toHaveBeenCalled();
  });
  it("shows no clear action while a bound shortcut is resting", () => {
    render(<ShortcutCapture {...makeProps({ value: "ctrl+alt+1" })} />);
    expect(screen.queryByRole("button", { name: "Clear shortcut" })).toBeNull();
    expect(screen.getByText("Ctrl+Alt+1")).toBeInTheDocument();
  });

  it("reveals clear and the below-field Escape cue while editing a bound shortcut", () => {
    render(<ShortcutCapture {...makeProps({ value: "ctrl+alt+1" })} />);

    fireEvent.click(screen.getByText("Ctrl+Alt+1"));

    expect(screen.getByText("Press shortcut")).toBeInTheDocument();
    expect(screen.getByText("Esc to cancel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear shortcut" })).toHaveTextContent("×");
  });

  it("uses a mount-site clear label when supplied", () => {
    render(
      <ShortcutCapture
        {...makeProps({
          value: "ctrl+alt+1",
          clearAriaLabel: "Clear Push to Talk shortcut",
        })}
      />,
    );
    fireEvent.click(screen.getByText("Ctrl+Alt+1"));
    expect(
      screen.getByRole("button", { name: "Clear Push to Talk shortcut" }),
    ).toBeInTheDocument();
  });

  it("clears once, stops capture, and exits the capture state", () => {
    const send = vi.fn(() => true);
    const onChange = vi.fn();
    render(
      <ShortcutCapture {...makeProps({ value: "ctrl+alt+1", send, onChange })} />,
    );
    fireEvent.click(screen.getByText("Ctrl+Alt+1"));
    fireEvent.click(screen.getByRole("button", { name: "Clear shortcut" }));

    expect(send).toHaveBeenLastCalledWith({ type: "shortcut.capture.stop" });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith("");
    expect(screen.queryByText("Press shortcut")).toBeNull();
    expect(screen.queryByText("Esc to cancel")).toBeNull();
  });

  it("does not reveal clear while capturing an empty shortcut", () => {
    render(<ShortcutCapture {...makeProps({ value: "" })} />);
    fireEvent.click(screen.getByText("Not set"));
    expect(screen.queryByRole("button", { name: "Clear shortcut" })).toBeNull();
    expect(screen.getByText("Esc to cancel")).toBeInTheDocument();
  });

  it("clearable=false suppresses clear while capturing an existing value", () => {
    render(
      <ShortcutCapture
        {...makeProps({ value: "ctrl+alt+1", clearable: false })}
      />,
    );
    fireEvent.click(screen.getByText("Ctrl+Alt+1"));
    expect(screen.queryByRole("button", { name: "Clear shortcut" })).toBeNull();
    expect(screen.getByText("Esc to cancel")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Capture lifecycle
// ---------------------------------------------------------------------------

describe("ShortcutCapture — capture lifecycle", () => {
  it("renders canonical Mac glyphs for resting and live shortcuts", () => {
    vi.spyOn(window.navigator, "platform", "get").mockReturnValue("MacIntel");
    const events = captureEvents();
    render(
      <ShortcutCapture
        {...makeProps({
          value: "right shift",
          subscribe: events.subscribe,
        })}
      />,
    );

    // Resting chips are decorative; the button's name carries the shortcut.
    const field = screen.getByRole("button", { name: "Right ⇧" });
    expect(field).toBeInTheDocument();
    fireEvent.click(field);
    act(() => {
      events.emit({
        type: "shortcut.capturing",
        keys: "shift+cmd+k",
        target: "primary",
      });
    });

    expect(screen.getByText("⌘")).toBeInTheDocument();
    expect(screen.getByText("K")).toBeInTheDocument();
  });

  it("clicking 'Not set' starts capture", () => {
    const send = vi.fn(() => true);
    render(<ShortcutCapture {...makeProps({ value: "", send })} />);
    fireEvent.click(screen.getByText("Not set"));
    expect(send).toHaveBeenCalledWith({
      type: "shortcut.capture.start",
      target: "primary",
    });
  });

  it("clicking the active field again cancels without clearing the old value", () => {
    const send = vi.fn(() => true);
    const onChange = vi.fn();
    render(
      <ShortcutCapture {...makeProps({ value: "ctrl+alt+1", send, onChange })} />,
    );

    fireEvent.click(screen.getByText("Ctrl+Alt+1"));
    fireEvent.click(screen.getByText("Press shortcut"));

    expect(send).toHaveBeenLastCalledWith({ type: "shortcut.capture.stop" });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText("Ctrl+Alt+1")).toBeInTheDocument();
  });

  it("committing a new combination replaces the old value", () => {
    const events = captureEvents();
    const onChange = vi.fn();
    render(
      <ShortcutCapture
        {...makeProps({
          value: "ctrl+alt+1",
          captureTarget: "toggle",
          subscribe: events.subscribe,
          onChange,
        })}
      />,
    );

    fireEvent.click(screen.getByText("Ctrl+Alt+1"));
    act(() => {
      events.emit({ type: "shortcut.captured", keys: "ctrl+shift+2", target: "toggle" });
    });

    expect(onChange).toHaveBeenCalledWith("ctrl+shift+2");
    expect(screen.queryByText("Press shortcut")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Esc-to-cancel
// ---------------------------------------------------------------------------

describe("ShortcutCapture — Esc-to-cancel", () => {
  it("pressing Esc cancels, preserves the value, and ignores a late Esc capture", () => {
    const events = captureEvents();
    const send = vi.fn(() => true);
    const onChange = vi.fn();
    render(
      <ShortcutCapture
        {...makeProps({
          value: "ctrl+alt+1",
          send,
          subscribe: events.subscribe,
          onChange,
        })}
      />,
    );

    fireEvent.click(screen.getByText("Ctrl+Alt+1"));
    expect(send).toHaveBeenCalledWith({
      type: "shortcut.capture.start",
      target: "primary",
    });

    fireEvent.keyDown(window, { key: "Escape" });
    act(() => {
      events.emit({ type: "shortcut.captured", keys: "esc", target: "primary" });
    });

    expect(send).toHaveBeenCalledWith({ type: "shortcut.capture.stop" });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText("Ctrl+Alt+1")).toBeInTheDocument();
  });
});
