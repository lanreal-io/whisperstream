import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LowDiskSpaceDialog } from "../LowDiskSpaceDialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeProps(
  overrides: Partial<Parameters<typeof LowDiskSpaceDialog>[0]> = {},
) {
  return {
    open: true,
    freeDiskGb: 1.2,
    onChooseFolder: vi.fn(),
    onSwitchToCloud: vi.fn(),
    onDismiss: vi.fn(),
    ...overrides,
  };
}

describe("LowDiskSpaceDialog", () => {
  it("uses the single Gemini CTA without supporting provider-price prose", () => {
    const onSwitchToCloud = vi.fn();
    render(<LowDiskSpaceDialog {...makeProps({ onSwitchToCloud })} />);

    fireEvent.click(
      screen.getByRole("button", { name: "Use Gemini's free tier" }),
    );
    expect(onSwitchToCloud).toHaveBeenCalledOnce();
    expect(screen.queryByText(/Cloud providers need an API key/)).toBeNull();
    expect(screen.queryByText(/OpenAI and Anthropic are paid/)).toBeNull();
  });

  it("omits the Gemini CTA when cloud fallback is unavailable", () => {
    render(
      <LowDiskSpaceDialog {...makeProps({ showCloudOption: false })} />,
    );

    expect(
      screen.queryByRole("button", { name: "Use Gemini's free tier" }),
    ).toBeNull();
  });
});
