/**
 * The language-change model guidance dialog.
 *
 * Both variants use the locked option-B copy from copy-review.md. The
 * same-family cases are the deliberate deviation: Parakeet's two artifacts
 * share one user-facing name, and the locked scope forbids exposing the
 * English-only / multilingual qualifiers outside bulk deletion, so those
 * transitions speak about the current and recommended SETUP instead of naming
 * one model twice.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MultilingualLanguageSwitchDialog } from "../MultilingualLanguageSwitchDialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function switchProps(
  overrides: Partial<Parameters<typeof MultilingualLanguageSwitchDialog>[0]> = {},
) {
  return {
    open: true,
    variant: "keep-or-switch" as const,
    currentModelName: "Qwen3 ASR",
    recommendedModelName: "NVIDIA Parakeet",
    targetLanguageName: "German",
    onKeepCurrentModel: vi.fn(),
    onSwitchToRecommended: vi.fn(),
    onDismiss: vi.fn(),
    ...overrides,
  };
}

describe("MultilingualLanguageSwitchDialog", () => {
  describe("keep-or-switch", () => {
    it("renders the locked copy with both model names and the target language", () => {
      render(<MultilingualLanguageSwitchDialog {...switchProps()} />);

      const dialog = screen.getByRole("dialog", {
        name: "Keep your current speech model?",
      });
      expect(dialog).toHaveAttribute("aria-modal", "true");
      expect(dialog).toHaveAccessibleDescription(
        "Qwen3 ASR supports German. For the best results, Whisperstream recommends NVIDIA Parakeet.",
      );
      expect(
        screen.getByRole("button", { name: "Keep Qwen3 ASR" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Switch to NVIDIA Parakeet" }),
      ).toBeInTheDocument();
    });

    it("focuses the neutral keep action when opened", () => {
      render(<MultilingualLanguageSwitchDialog {...switchProps()} />);

      expect(screen.getByRole("button", { name: "Keep Qwen3 ASR" })).toHaveFocus();
    });

    it("runs the matching action from each button", () => {
      const onKeepCurrentModel = vi.fn();
      const onSwitchToRecommended = vi.fn();
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({ onKeepCurrentModel, onSwitchToRecommended })}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: "Keep Qwen3 ASR" }));
      fireEvent.click(
        screen.getByRole("button", { name: "Switch to NVIDIA Parakeet" }),
      );

      expect(onKeepCurrentModel).toHaveBeenCalledOnce();
      expect(onSwitchToRecommended).toHaveBeenCalledOnce();
    });

    it("maps Escape and backdrop clicks to abandoning the language change", () => {
      const onDismiss = vi.fn();
      const onKeepCurrentModel = vi.fn();
      const onSwitchToRecommended = vi.fn();
      const { container } = render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({ onDismiss, onKeepCurrentModel, onSwitchToRecommended })}
        />,
      );

      fireEvent.keyDown(window, { key: "Escape" });
      fireEvent.click(container.querySelector(".absolute.inset-0")!);

      expect(onDismiss).toHaveBeenCalledTimes(2);
      expect(onKeepCurrentModel).not.toHaveBeenCalled();
      expect(onSwitchToRecommended).not.toHaveBeenCalled();
    });

    it("does not bind Enter to the consequential switch action", () => {
      const onSwitchToRecommended = vi.fn();
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({ onSwitchToRecommended })}
        />,
      );

      fireEvent.keyDown(window, { key: "Enter" });

      expect(onSwitchToRecommended).not.toHaveBeenCalled();
    });
  });

  describe("incompatible", () => {
    const incompatible = {
      variant: "incompatible" as const,
      currentModelName: "NVIDIA Parakeet",
      recommendedModelName: "Qwen3 ASR",
      targetLanguageName: "Japanese",
    };

    it("renders the locked incompatible copy and offers no keep-current action", () => {
      render(
        <MultilingualLanguageSwitchDialog {...switchProps(incompatible)} />,
      );

      const dialog = screen.getByRole("dialog", {
        name: "A model change is needed",
      });
      expect(dialog).toHaveAccessibleDescription(
        "Your current model does not support Japanese. Whisperstream will switch to Qwen3 ASR.",
      );
      expect(
        screen.getByRole("button", { name: "Switch to Qwen3 ASR" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Keep NVIDIA Parakeet" }),
      ).not.toBeInTheDocument();
    });

    it("focuses the safe cancel action and maps it to dismissal", () => {
      const onDismiss = vi.fn();
      const onKeepCurrentModel = vi.fn();
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({ ...incompatible, onDismiss, onKeepCurrentModel })}
        />,
      );

      const cancel = screen.getByRole("button", {
        name: "Keep current language",
      });
      expect(cancel).toHaveFocus();

      fireEvent.click(cancel);
      fireEvent.keyDown(window, { key: "Escape" });

      expect(onDismiss).toHaveBeenCalledTimes(2);
      expect(onKeepCurrentModel).not.toHaveBeenCalled();
    });
  });

  // Both Parakeet artifacts are `NVIDIA Parakeet` to the user. Naming either
  // side would read as one model being recommended over itself, and naming the
  // artifacts is forbidden outside bulk deletion.
  describe("same-family transitions", () => {
    const sameFamily = {
      currentModelName: "NVIDIA Parakeet",
      recommendedModelName: "NVIDIA Parakeet",
    };

    it("keeps Automatic keep-or-switch generic for multilingual Parakeet moving to English", () => {
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({ ...sameFamily, targetLanguageName: "English" })}
        />,
      );

      const dialog = screen.getByRole("dialog", {
        name: "Keep your current speech model?",
      });
      expect(dialog).toHaveAccessibleDescription(
        "Your current setup supports English. For the best results, Whisperstream recommends a different setup.",
      );
      expect(
        screen.getByRole("button", { name: "Keep current setup" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Use recommended setup" }),
      ).toBeInTheDocument();
      expect(dialog.textContent).not.toContain("English-only");
      expect(dialog.textContent).not.toContain("Multilingual");
      expect(dialog.textContent).not.toContain("Switch to NVIDIA Parakeet");
    });

    it("keeps Manual incompatible generic for English-only Parakeet moving to French", () => {
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({
            ...sameFamily,
            variant: "incompatible",
            targetLanguageName: "French",
          })}
        />,
      );

      const dialog = screen.getByRole("dialog", {
        name: "A model change is needed",
      });
      expect(dialog).toHaveAccessibleDescription(
        "Your current setup does not support French. Whisperstream will switch to its recommended setup.",
      );
      expect(
        screen.getByRole("button", { name: "Keep current language" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Use recommended setup" }),
      ).toBeInTheDocument();
      expect(dialog.textContent).not.toContain("English-only");
      expect(dialog.textContent).not.toContain("Multilingual");
    });

    it("never names one model on both sides of the decision", () => {
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({ ...sameFamily, targetLanguageName: "English" })}
        />,
      );

      const text = screen.getByRole("dialog").textContent ?? "";
      expect(text.match(/NVIDIA Parakeet/g)).toBeNull();
    });

    it("preserves the routing actions in the generic presentation", () => {
      const onKeepCurrentModel = vi.fn();
      const onSwitchToRecommended = vi.fn();
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({
            ...sameFamily,
            targetLanguageName: "English",
            onKeepCurrentModel,
            onSwitchToRecommended,
          })}
        />,
      );

      expect(
        screen.getByRole("button", { name: "Keep current setup" }),
      ).toHaveFocus();
      fireEvent.click(screen.getByRole("button", { name: "Keep current setup" }));
      fireEvent.click(
        screen.getByRole("button", { name: "Use recommended setup" }),
      );

      expect(onKeepCurrentModel).toHaveBeenCalledOnce();
      expect(onSwitchToRecommended).toHaveBeenCalledOnce();
    });

    it("uses no em dashes", () => {
      render(
        <MultilingualLanguageSwitchDialog
          {...switchProps({ ...sameFamily, targetLanguageName: "English" })}
        />,
      );

      expect(screen.getByRole("dialog").textContent).not.toContain("—");
    });
  });

  it("renders nothing while closed", () => {
    render(<MultilingualLanguageSwitchDialog {...switchProps({ open: false })} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
