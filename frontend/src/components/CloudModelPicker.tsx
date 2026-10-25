import { useState } from "react";
import { Dropdown, type DropdownOption } from "./Dropdown";
import { HoverTooltip } from "./HoverTooltip";

interface Props {
  value: string;
  catalog: DropdownOption<string>[];
  saved: string[];
  onChange: (value: string, saved: string[]) => void;
}

/** Provider-scoped model history; the parent saves it with ordinary settings. */
export function CloudModelPicker({ value, catalog, saved, onChange }: Props) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const curated = new Set(catalog.map((item) => item.value));
  // Keep upgraded selections available even before the first config save.
  const custom = [...new Set([...saved, value].filter((id) => id && !curated.has(id)))];
  const options = [
    ...catalog,
    ...custom.map((id) => ({ value: id, label: id })),
  ];
  const add = () => {
    const id = draft.trim();
    if (!id || id.length > 128 || /[\u0000-\u001f]/.test(id)) return;
    onChange(id, [...new Set([...custom, id])]);
    setAdding(false);
    setDraft("");
  };
  return (
    <div className="space-y-2">
      <Dropdown
        value={value}
        options={options}
        onChange={(id) => onChange(id, custom)}
        ariaLabel="Model"
        placeholder="Choose a model"
        triggerClassName="w-full"
        optionActionsFocusable
        renderOptionAction={(option) => custom.includes(option.value) ? (
          <HoverTooltip content="Delete saved model">
            <button
              type="button"
              aria-label={`Delete saved model ${option.value}`}
              onClick={(event) => {
                event.stopPropagation();
                onChange(value === option.value ? "" : value, custom.filter((id) => id !== option.value));
              }}
              className="w-6 h-6 rounded-full flex items-center justify-center text-steel hover:text-rose hover:bg-[var(--surface-hover)] transition-colors cursor-pointer"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6" />
              </svg>
            </button>
          </HoverTooltip>
        ) : null}
      />
      {adding ? (
        <div className="flex items-center gap-2">
          <input
            autoFocus
            aria-label="Custom model name"
            value={draft}
            maxLength={128}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") { event.preventDefault(); add(); }
              if (event.key === "Escape") { setAdding(false); setDraft(""); }
            }}
            placeholder="Enter a model ID"
            className="min-w-0 flex-1 h-9 rounded-[var(--radius-input)] px-3 text-[13px] text-slate placeholder:text-steel/40"
            style={{ background: "var(--surface-control)", border: "1px solid var(--border-hairline)" }}
          />
          <button type="button" onClick={add} disabled={!draft.trim()} className="text-[12px] text-frost-deep disabled:opacity-40 cursor-pointer">Add</button>
          <button type="button" onClick={() => { setAdding(false); setDraft(""); }} className="text-[12px] text-steel cursor-pointer">Cancel</button>
        </div>
      ) : (
        <button type="button" onClick={() => setAdding(true)} className="text-[11px] text-steel hover:text-frost-deep transition-colors cursor-pointer">
          Add custom model
        </button>
      )}
    </div>
  );
}
