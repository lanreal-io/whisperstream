import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PrimaryButton } from "../PrimaryButton";

afterEach(cleanup);

describe("PrimaryButton frost depth", () => {
  it("uses a restrained frost-tinted outer glow in idle and hover states", () => {
    render(<PrimaryButton>Continue</PrimaryButton>);
    const button = screen.getByRole("button", { name: "Continue" });

    expect(button.style.boxShadow).toContain("var(--color-frost)");
    expect(button.style.boxShadow).not.toContain("0 0 12px rgba(255");

    fireEvent.mouseEnter(button);
    expect(button.style.boxShadow).toContain("var(--color-frost)");
    expect(button.style.boxShadow).not.toContain("0 0 16px rgba(255");
  });
});
