interface CustomCheckboxProps {
  checked: boolean;
  onChange: (v: boolean) => void;
}

export function CustomCheckbox({ checked, onChange }: CustomCheckboxProps) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      onKeyDown={(e) => {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          onChange(!checked);
        }
      }}
      className="w-4 h-4 rounded-[5px] shrink-0 flex items-center justify-center cursor-pointer"
      style={{
        // Resting (unchecked) reads clearly as an empty box: a steel-tinted
        // outline (AA-legible on both the light glass card and the dark card)
        // over a faint control fill. Checked flips to the accent-fill gradient
        // with a white check + frost bloom — an unmistakable checked state.
        background: checked ? "var(--accent-fill)" : "var(--surface-control)",
        border: checked
          ? "1px solid var(--border-accent)"
          : "1px solid color-mix(in srgb, var(--color-steel) 55%, transparent)",
        boxShadow: checked
          ? "var(--shadow-frost-glow)"
          : "inset 0 1px 2px rgba(0, 0, 0, 0.04)",
        transition:
          "background-color 120ms ease, border-color 120ms ease, box-shadow 120ms ease",
      }}
    >
      {checked && (
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          stroke="white"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M2 5l2 2 4-4" />
        </svg>
      )}
    </button>
  );
}

export default CustomCheckbox;
