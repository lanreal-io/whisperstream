// @vitest-environment node

/**
 * Tests for the rangeFill helper that drives the global range-slider fill
 * via the --range-fill CSS custom property (see globals.css's
 * "Range slider" comment block and design.md's "Slider consolidation"
 * section).
 */
import { describe, it, expect } from "vitest";
import { rangeFill } from "./rangeFill";

describe("rangeFill", () => {
  it("returns 0% at the minimum", () => {
    expect(rangeFill(0.5, 0.5, 2)).toEqual({ "--range-fill": "0%" });
  });

  it("returns 100% at the maximum", () => {
    expect(rangeFill(2, 0.5, 2)).toEqual({ "--range-fill": "100%" });
  });

  it("returns a rounded integer percent partway through the range", () => {
    // (0.75 - 0.5) / (2 - 0.5) = 0.1666... -> 16.67% -> rounds to 17%
    expect(rangeFill(0.75, 0.5, 2)).toEqual({ "--range-fill": "17%" });
  });

  it("handles a 0-1 range (volume-style sliders)", () => {
    expect(rangeFill(0.7, 0, 1)).toEqual({ "--range-fill": "70%" });
  });

  it("handles a 0-15 integer range (reply window)", () => {
    expect(rangeFill(5, 0, 15)).toEqual({ "--range-fill": "33%" });
  });

  it("clamps a value below the minimum to 0%", () => {
    expect(rangeFill(-1, 0, 1)).toEqual({ "--range-fill": "0%" });
  });

  it("clamps a value above the maximum to 100%", () => {
    expect(rangeFill(1.5, 0, 1)).toEqual({ "--range-fill": "100%" });
  });

  it("is safe when max === min, returning 0%", () => {
    expect(rangeFill(5, 5, 5)).toEqual({ "--range-fill": "0%" });
  });
});
