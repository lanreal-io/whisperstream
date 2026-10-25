import { useEffect, useId, useState } from "react";
import { rangeFill } from "../../lib/rangeFill";
import { SliderField } from "../SliderField";

const TIERS = [0, 3, 6, 10, 15, 20, 30];

/** Editable seconds with local invalid drafts; only valid integers persist.
 *  `tiered` seats a tier slider and the box in one `SliderField`; the box then
 *  says the value, so the tick labels are gone (owner, 2026-10-25). */
export function SecondsControl({ label, value, min = 0, max = 30, tiered = false, onChange }: {
  label: string; value: number; min?: number; max?: number;
  tiered?: boolean; onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  const id = useId();
  useEffect(() => setDraft(String(value)), [value]);
  const valid = /^\d+$/.test(draft) && Number(draft) >= min && Number(draft) <= max;
  // Interpolate custom values between tiers so the freeform field never jumps.
  const tier = TIERS.findIndex((tick) => tick >= value);
  const position = tier <= 0 ? 0 : tier - 1 + (value - TIERS[tier - 1]) / (TIERS[tier] - TIERS[tier - 1]);
  const field = (
    <input type="text" inputMode="numeric" aria-label={`${label} seconds`} aria-invalid={!valid}
      aria-describedby={!valid ? id : undefined} value={draft}
      className={tiered
        ? "w-12 rounded-lg border border-[var(--border-hairline-steel)] bg-[var(--surface-raised)] px-1.5 py-1 text-right text-[12px] font-medium text-slate tabular-nums"
        : "w-14 rounded-lg border border-[var(--border-hairline)] bg-[var(--surface-control)] px-2 py-1.5 text-right text-slate tabular-nums"}
      onChange={(event) => { const next = event.target.value; setDraft(next); if (/^\d+$/.test(next) && Number(next) >= min && Number(next) <= max) onChange(Number(next)); }} />
  );
  return <div className="flex flex-col items-end gap-2">
    {tiered ? (
      <SliderField className="w-[240px]" value={<label className="flex shrink-0 items-center gap-1 text-[11px] text-steel">{field}s</label>}>
        <input type="range" aria-label={label} aria-valuetext={value === 0 ? "Off" : `${value} seconds`}
          min={0} max={TIERS.length - 1} step="any" value={position}
          style={rangeFill(position, 0, TIERS.length - 1)}
          onChange={(event) => { const next = TIERS[Math.round(Number(event.target.value))]; setDraft(String(next)); onChange(next); }}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
            event.preventDefault();
            const up = event.key === "ArrowRight" || event.key === "ArrowUp";
            const next = up ? TIERS.find((tick) => tick > value) ?? 30 : [...TIERS].reverse().find((tick) => tick < value) ?? 0;
            setDraft(String(next)); onChange(next);
          }} />
      </SliderField>
    ) : (
      <label className="flex items-center gap-1.5 text-[11px] text-steel">
        {field}
        seconds
      </label>
    )}
    {!valid && <p id={id} role="alert" className="text-[11px] text-rose">Enter a whole number from {min} to {max}.</p>}
  </div>;
}
