import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SemanticDiffLegend, SemanticDiffText } from "./SemanticDiff";

afterEach(() => cleanup());

describe("SemanticDiff", () => {
  it("keeps replaced source text visible before the revised text", () => {
    const { container } = render(
      <p>
        <SemanticDiffText original="Meet Tuesday morning." revised="Meet Wednesday morning." />
      </p>,
    );

    const removed = container.querySelector('[data-diff-op="removed"]');
    const added = container.querySelector('[data-diff-op="added"]');
    expect(removed).toHaveTextContent("Tuesday");
    expect(added).toHaveTextContent("Wednesday");
    expect(removed).not.toBeNull();
    expect(added).not.toBeNull();
    expect(removed!.parentElement).toHaveClass("select-text");
    expect(
      removed!.compareDocumentPosition(added!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("uses text labels as well as color in the shared legend", () => {
    render(<SemanticDiffLegend />);
    expect(screen.getByText("Added")).toBeInTheDocument();
    expect(screen.getByText("Removed")).toBeInTheDocument();
  });
});
