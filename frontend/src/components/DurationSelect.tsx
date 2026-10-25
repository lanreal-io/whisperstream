import { Dropdown, type DropdownOption } from "./Dropdown";

interface DurationSelectProps {
  /** Current value in seconds. 0 = Never (keep loaded). */
  value: number;
  onChange: (seconds: number) => void;
  /** Row purpose for screen readers, e.g. "Unload model when idle". The current
   *  selected value is appended automatically by Dropdown. */
  ariaLabel?: string;
}

const OPTIONS: DropdownOption<number>[] = [
  { label: "Never", value: 0 },
  { label: "5 min", value: 300 },
  { label: "10 min", value: 600 },
  { label: "15 min", value: 900 },
  { label: "30 min", value: 1800 },
  { label: "1 hour", value: 3600 },
];

/**
 * Idle-unload duration picker for the model-offload settings. Thin wrapper over
 * the shared {@link Dropdown}; values are seconds (0 = Never). A config value
 * that matches no option (e.g. a hand-edited timeout) falls back to "Never".
 */
export function DurationSelect({ value, onChange, ariaLabel }: DurationSelectProps) {
  return (
    <Dropdown
      value={value}
      options={OPTIONS}
      onChange={onChange}
      ariaLabel={ariaLabel}
      triggerClassName="w-[112px]"
      panelWidth={140}
      placeholder="Never"
    />
  );
}
