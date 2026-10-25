/**
 * Vitest + Testing Library coverage for SiteGlyph (RunningAppPicker.tsx).
 *
 * SiteGlyph gained an OPTIONAL `icon` prop (a favicon data-URI sourced LOCALLY
 * from the browser cache by the backend; SiteGlyph itself never fetches). Tests
 * lock the back-compatible contract:
 *   - with no `icon`: renders the inline globe SVG, no <img> (existing callers
 *     are visually unchanged);
 *   - with `icon` set: renders an <img> with that src and no globe;
 *   - on <img> onError: falls back to the globe glyph;
 *   - the chip stays aria-hidden and rounded-full (the app-vs-site shape rule).
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup, fireEvent } from "@testing-library/react";
import { SiteGlyph } from "../RunningAppPicker";

afterEach(() => {
  cleanup();
});

const DATA_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==";

describe("SiteGlyph", () => {
  it("renders the globe SVG and no <img> when no icon is passed", () => {
    const { container } = render(<SiteGlyph />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("the chip is aria-hidden and rounded-full", () => {
    const { container } = render(<SiteGlyph />);
    const chip = container.querySelector("span");
    expect(chip?.getAttribute("aria-hidden")).toBe("true");
    expect(chip?.className).toContain("rounded-full");
  });

  it("renders an <img> with the data-URI src (and no globe) when icon is set", () => {
    const { container } = render(<SiteGlyph icon={DATA_URI} />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toBe(DATA_URI);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("treats a null icon exactly like no icon (globe, no img)", () => {
    const { container } = render(<SiteGlyph icon={null} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("falls back to the globe glyph when the <img> fails to decode", () => {
    const { container } = render(<SiteGlyph icon={DATA_URI} />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    fireEvent.error(img as HTMLImageElement);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).not.toBeNull();
  });
});
