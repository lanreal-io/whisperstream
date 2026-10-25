import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SetupKeyChip } from "./SetupKeyChip";

const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";

function chips(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>("[data-setup-key]"));
}

describe("SetupKeyChip", () => {
  it("splits on + and title-cases each token", () => {
    const { container } = render(<SetupKeyChip keys="ctrl+alt+right shift" />);
    expect(chips(container).map((c) => c.textContent)).toEqual([
      "Ctrl",
      "Alt",
      "Right Shift",
    ]);
  });

  it("renders the inherited Not set string when there are no keys", () => {
    const { container } = render(<SetupKeyChip keys="" />);
    expect(within(container).getByText("Not set")).toBeInTheDocument();
    expect(chips(container)).toHaveLength(0);
  });

  // The three deltas from `KeyChip`, which is why this component exists at all.
  // See design.md decision 6: on `--surface-modal` in Light, KeyChip's
  // white-family border vanishes, and in Dark its fill matches
  // `--surface-inset-card` exactly. Only the steel ring draws the shape on both
  // grounds in both themes.
  it("rings with the steel border, not --border-hairline", () => {
    const { container } = render(<SetupKeyChip keys="ctrl+alt" />);
    for (const chip of chips(container)) {
      expect(chip).toHaveStyle({ border: STEEL_BORDER });
      expect(chip.style.border).not.toContain("--border-hairline");
    }
  });

  it("uses the --rim-specular token for the specular rim", () => {
    const { container } = render(<SetupKeyChip keys="ctrl" />);
    const [chip] = chips(container);
    expect(chip.style.boxShadow).toContain("inset 0 1px 0 var(--rim-specular)");
    expect(chip.style.boxShadow).not.toContain("255");
  });

  it("drops KeyChip's frost drop glow", () => {
    const { container } = render(<SetupKeyChip keys="ctrl" />);
    const [chip] = chips(container);
    expect(chip.style.boxShadow).not.toContain("0 2px 8px");
    expect(chip.style.boxShadow).not.toContain("--color-frost");
  });

  it("fills with --surface-control", () => {
    const { container } = render(<SetupKeyChip keys="ctrl" />);
    expect(chips(container)[0]).toHaveStyle({
      background: "var(--surface-control)",
    });
  });

  // Accessibility Notes in copy-review.md: an inline chip must read
  // continuously inside its sentence.
  it("hides the + separators from screen readers", () => {
    const { container } = render(<SetupKeyChip keys="ctrl+alt+space" />);
    const separators = Array.from(
      container.querySelectorAll<HTMLElement>("[data-setup-key-separator]"),
    );
    expect(separators).toHaveLength(2);
    for (const separator of separators) {
      expect(separator).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("adds no aria-label anywhere, so the sentence is not interrupted", () => {
    const { container } = render(<SetupKeyChip keys="ctrl+alt+space" />);
    expect(container.querySelectorAll("[aria-label]")).toHaveLength(0);
  });
});
