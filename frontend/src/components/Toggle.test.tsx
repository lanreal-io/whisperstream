import type { ComponentPropsWithoutRef } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("framer-motion", () => ({
  motion: {
    span: ({
      initial,
      animate: _animate,
      transition: _transition,
      ...props
    }: ComponentPropsWithoutRef<"span"> & {
      initial?: boolean;
      animate?: unknown;
      transition?: unknown;
    }) => (
      <span
        {...props}
        data-testid="toggle-thumb"
        data-motion-initial={String(initial)}
      />
    ),
  },
}));

import { Toggle } from "./Toggle";

describe("Toggle", () => {
  it("paints the current state immediately instead of animating from a faded mount", () => {
    render(
      <Toggle
        checked
        onChange={vi.fn()}
        ariaLabel="Example setting"
      />,
    );

    expect(screen.getByRole("switch", { name: "Example setting" })).toBeEnabled();
    expect(screen.getByTestId("toggle-thumb")).toHaveAttribute(
      "data-motion-initial",
      "false",
    );
  });
});
