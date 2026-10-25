import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RecoverySetupGate } from "../RecoverySetupGate";

afterEach(cleanup);

describe("RecoverySetupGate", () => {
  it("asks for setup with platform copy and the saving status", () => {
    const onSetUp = vi.fn();
    render(<RecoverySetupGate platform="windows" recordCount={142} onSetUp={onSetUp} />);
    expect(screen.getByRole("heading", { name: "Finish recovery setup" })).toBeInTheDocument();
    expect(screen.getByText(
      "Add a recovery key to see your transcripts. Without one, they can't be restored if this PC loses its encryption key.",
    )).toBeInTheDocument();
    expect(screen.getByText("Saving is on. 142 transcripts saved.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Set up recovery key" }));
    expect(onSetUp).toHaveBeenCalledOnce();
  });

  it("uses the singular status and omits it without a count", () => {
    const { rerender } = render(<RecoverySetupGate platform="mac" recordCount={1} onSetUp={vi.fn()} />);
    expect(screen.getByText("Saving is on. 1 transcript saved.")).toBeInTheDocument();
    rerender(<RecoverySetupGate platform="mac" recordCount={null} onSetUp={vi.fn()} />);
    expect(screen.queryByText(/Saving is on/)).toBeNull();
  });
});
