import { Dropdown, type DropdownOption } from "./Dropdown";

interface TypingDelaySelectProps {
  /** Current value in milliseconds. 0 = None (no inter-key pause). */
  value: number;
  onChange: (ms: number) => void;
  /** Screen-reader label for the trigger, e.g. "Inter-key delay". The current
   *  selected value is appended automatically by Dropdown. */
  ariaLabel?: string;
  /** When true, the trigger is non-interactive and dimmed. Passed through to
   *  Dropdown (unlike DurationSelect, which lacks this prop). */
  disabled?: boolean;
}

const OPTIONS: DropdownOption<number>[] = [
  { label: "None", value: 0 },
  { label: "5 ms", value: 5 },
  { label: "10 ms", value: 10 },
  { label: "15 ms", value: 15 },
  { label: "25 ms", value: 25 },
];

/**
 * Inter-key pause picker for the keystroke injection settings. Thin wrapper
 * over the shared {@link Dropdown}; values are milliseconds (0 = None). A
 * config value that matches no option (e.g. a hand-edited delay) falls back
 * to "None". Threads `disabled` through to Dropdown, unlike DurationSelect.
 */
export function TypingDelaySelect({ value, onChange, ariaLabel, disabled }: TypingDelaySelectProps) {
  return (
    <Dropdown
      value={value}
      options={OPTIONS}
      onChange={onChange}
      ariaLabel={ariaLabel}
      triggerClassName="w-[112px]"
      panelWidth={140}
      placeholder="None"
      disabled={disabled}
    />
  );
}
