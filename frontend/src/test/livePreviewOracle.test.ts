// @vitest-environment node

import { describe, expect, it } from "vitest";

import {
  additivePrefixDuplicatePositions,
  exactVisibleGoldAlignment,
  missingExactGoldPositions,
  shouldProtectGoldAnchor,
} from "../../scripts/live-preview-oracle";


describe("Live Preview saved-gold oracle", () => {
  it("does not protect an exact occurrence while it is mutable", () => {
    expect(
      shouldProtectGoldAnchor({
        exactVisiblePosition: 2,
        visibleText: "one two three four",
        mutableStartCharCount: "one two".length,
      }),
    ).toBe(false);
  });

  it("protects a word on the exact checkpoint where it crosses the boundary", () => {
    const visible = "one two three four";
    expect(
      shouldProtectGoldAnchor({
        exactVisiblePosition: 2,
        visibleText: visible,
        mutableStartCharCount: "one two three".length,
      }),
    ).toBe(true);
  });

  it("does not let unrelated equal-length text satisfy exact coverage", () => {
    const alignment = exactVisibleGoldAlignment(
      "one two three four",
      "alpha beta gamma delta",
    );

    expect(alignment.exactCovered).toEqual([
      false,
      false,
      false,
      false,
    ]);
    expect(missingExactGoldPositions(alignment, 4)).toEqual([1, 2, 3, 4]);
  });

  it("keeps repeated word occurrences monotonic and one-to-one", () => {
    const alignment = exactVisibleGoldAlignment(
      "go go stop go",
      "go stop go",
    );

    expect(alignment.exactVisiblePositions).toEqual([0, -1, 1, 2]);
    expect(missingExactGoldPositions(alignment, 4)).toEqual([2]);
  });

  it("reports an unmatched prefix beside its exact completed occurrence", () => {
    expect(
      additivePrefixDuplicatePositions(
        "Hey, what's up? How's it going?",
        "Hey, what's up? how How's it going?",
      ),
    ).toEqual([4]);
  });

  it("does not report legitimate repeated or prefix-adjacent speech", () => {
    expect(
      additivePrefixDuplicatePositions(
        "Tell me how how's that possible",
        "Tell me how how's that possible",
      ),
    ).toEqual([]);
    expect(
      additivePrefixDuplicatePositions(
        "go go now",
        "go go now",
      ),
    ).toEqual([]);
  });

  it("does not jump a repeated word to a later gold occurrence", () => {
    const alignment = exactVisibleGoldAlignment(
      "no pause pause it still drops words like in middle I think it had",
      "no pause it still drops",
    );

    expect(alignment.furthestGoldPosition).toBe(6);
    expect(alignment.exactVisiblePositions[3]).toBe(2);
    expect(alignment.exactVisiblePositions[11]).toBe(-1);
  });

  it("treats substitutions as diagnostics, not verdict coverage", () => {
    const alignment = exactVisibleGoldAlignment(
      "alpha beta gamma",
      "alpha bravo gamma",
    );

    expect(missingExactGoldPositions(alignment, 3)).toEqual([2]);
    expect(alignment.alignedVisiblePositions[1]).toBe(1);
  });

  it("distinguishes a deletion from a one-to-one substitution", () => {
    const alignment = exactVisibleGoldAlignment(
      "alpha beta gamma",
      "alpha gamma",
    );

    expect(alignment.alignedVisiblePositions[1]).toBe(-1);
  });

  it("allows mutable correction while retaining exact protected occurrences", () => {
    const initial = "one two wrong ending";
    const corrected = "one two corrected ending";
    const initialAlignment = exactVisibleGoldAlignment(
      "one two corrected ending",
      initial,
    );
    const protectedGold = initialAlignment.exactCovered.map(
      (covered, index) =>
        covered &&
        shouldProtectGoldAnchor({
          exactVisiblePosition:
            initialAlignment.exactVisiblePositions[index],
          visibleText: initial,
          mutableStartCharCount: "one two".length,
        }),
    );
    const correctedAlignment = exactVisibleGoldAlignment(
      "one two corrected ending",
      corrected,
    );

    expect(
      missingExactGoldPositions(
        correctedAlignment,
        protectedGold.length,
        protectedGold,
      ),
    ).toEqual([]);
  });
});
