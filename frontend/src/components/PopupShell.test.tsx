import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { PopupShell } from "./PopupShell";

afterEach(() => {
  cleanup();
  document.documentElement.style.removeProperty("background");
  document.body.style.removeProperty("background");
  window.history.replaceState({}, "", "/");
  vi.unstubAllGlobals();
});

describe("PopupShell", () => {
  it("uses the desktop-popup surface instead of the in-app modal surface", () => {
    render(
      <PopupShell width={400}>
        <p>Popup content</p>
      </PopupShell>,
    );

    const card = screen.getByText("Popup content").parentElement;
    expect(card).toHaveStyle({
      width: "400px",
      background: "var(--surface-popup)",
    });
  });

  it("marks exact-state popup fixtures with stable root, surface, and readiness attributes", async () => {
    vi.stubGlobal(
      "requestAnimationFrame",
      (callback: FrameRequestCallback) =>
        window.setTimeout(() => callback(performance.now()), 0),
    );
    vi.stubGlobal("cancelAnimationFrame", (id: number) =>
      window.clearTimeout(id),
    );
    window.history.replaceState(
      {},
      "",
      "/debug/ui-qa?state=popup.update&theme=dark",
    );

    render(
      <PopupShell width={400}>
        <p>Popup content</p>
      </PopupShell>,
    );

    const root = document.querySelector("[data-ui-qa-root]");
    expect(root).toHaveAttribute("data-ui-qa-state", "popup.update");
    expect(
      document.querySelector('[data-ui-qa-surface="popup-card"]'),
    ).not.toBeNull();
    expect(
      document.querySelector('[data-ui-qa-floating="popup-card"]'),
    ).not.toBeNull();
    await waitFor(() =>
      expect(root).toHaveAttribute("data-ui-qa-ready", "true"),
    );
  });
});
