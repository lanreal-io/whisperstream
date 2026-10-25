import type { MouseEventHandler, ReactNode } from "react";
import { PrimaryButton } from "../PrimaryButton";

// Primary advance/finish control in the onboarding footer. Thin wrapper over the
// shared PrimaryButton so the welcome flow's CTA matches the
// rest of the app's primary buttons. The `animated` flag drives the "delight
// moment" on first transcription in step 3 (opacity 40 -> 100 + scale 0.96 -> 1).

interface ContinueButtonProps {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  animated?: boolean;
}

export function ContinueButton({
  label,
  onClick,
  disabled = false,
  animated = false,
}: ContinueButtonProps) {
  return (
    <PrimaryButton
      onClick={onClick}
      disabled={disabled}
      animated={animated}
      className="px-7 py-2.5 text-[13px]"
    >
      {label}
    </PrimaryButton>
  );
}

interface GhostLinkProps {
  disabled?: boolean;
  onClick: MouseEventHandler<HTMLButtonElement>;
  children: ReactNode;
}

export function GhostLink({ onClick, children, disabled }: GhostLinkProps) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="disabled:opacity-50 text-[12px] text-steel/70 hover:text-steel transition-colors duration-150 cursor-pointer"
    >
      {children}
    </button>
  );
}
