import { motion } from "framer-motion";

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** When true, the switch is non-interactive and dimmed. Used when a
   *  dependent setting has no effect (e.g. Save audio while history is off). */
  disabled?: boolean;
  /** Explicit accessible name for switches whose visual label is not
   *  programmatically associated with the button. */
  ariaLabel?: string;
  /** Id of an element (usually the row's own description) that supplies
   *  additional accessible context beyond `ariaLabel`. */
  ariaDescribedBy?: string;
}

export function Toggle({
  checked,
  onChange,
  disabled = false,
  ariaLabel,
  ariaDescribedBy,
}: ToggleProps) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      aria-describedby={ariaDescribedBy}
      aria-disabled={disabled || undefined}
      disabled={disabled}
      onClick={() => {
        if (disabled) return;
        onChange(!checked);
      }}
      className={`relative w-12 h-[26px] rounded-full shrink-0 transition-all duration-300 ${
        disabled ? "opacity-40 cursor-not-allowed" : "cursor-pointer"
      }`}
      style={{
        background: checked
          ? "var(--accent-fill)"
          : "var(--surface-control)",
      }}
    >
      <motion.span
        className="absolute top-[3px] w-5 h-5 rounded-full"
        initial={false}
        animate={{
          left: checked ? 25 : 3,
          backgroundColor: checked ? "#ffffff" : "rgba(255, 255, 255, 0.05)",
          boxShadow: checked
            ? `0 2px 8px var(--border-accent)`
            : `inset 0 0 0 2px var(--border-accent)`,
        }}
        transition={{ type: "spring", stiffness: 500, damping: 30 }}
      />
    </button>
  );
}
