import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LanguagePopup } from "./LanguagePopup";

afterEach(cleanup);
HTMLElement.prototype.scrollIntoView = vi.fn();

describe("LanguagePopup platform availability", () => {
  it("keeps unsupported languages visible but inert with an accessible reason", () => {
    const reason = "This language needs a speech model that is not available on this platform.";
    render(
      <LanguagePopup
        open
        selectedCode="en"
        onSelect={vi.fn()}
        onClose={vi.fn()}
        isLanguageAvailable={(code) => code !== "ja"}
        unavailableReason={reason}
      />,
    );

    const japanese = screen.getByRole("button", { name: "Japanese" });
    expect(japanese).toBeDisabled();
    expect(japanese).toHaveAttribute("aria-description", reason);
    expect(screen.getByRole("button", { name: "English" })).toBeEnabled();
  });
});
