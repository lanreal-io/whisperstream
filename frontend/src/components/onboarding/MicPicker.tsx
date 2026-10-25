import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { getDevices, type AudioDevice } from "../../lib/api/system";
import { type AppConfig } from "../../lib/api/config";
import { errorType } from "../../lib/logCapture";
import {
  inputDevicePatch,
  inputDeviceValue,
  savedInputDeviceAvailability,
  savedInputDeviceLabel,
  selectedInputDevice,
  type InputDeviceInventoryStatus,
} from "../../lib/inputDeviceSelection";

interface MicPickerProps {
  value: string;
  savedLabel?: string;
  savedAvailable?: boolean;
  selectionMode?: AppConfig["audio_input_device_selection_mode"];
  onChange: (patch: Partial<AppConfig>) => void;
}

// Collapsed-by-default microphone disclosure for the onboarding test step.
//
// The defaults work for >95% of users; this control is intentionally
// de-emphasized so the test step requires zero action when the system
// default mic is fine. Click the caret to expand the list and pick a
// different device.
//
// Locked design (design.md §Components §MicPicker):
//   - Caret + "Microphone: <name>" label at 11px text-steel/60
//   - Click expands; first expand fetches devices via getDevices()
//   - Selecting a device passes the platform-specific persisted value and collapses
//   - Does NOT call saveConfig — the parent (Finish button) persists
export function MicPicker({
  value,
  savedLabel,
  savedAvailable,
  selectionMode,
  onChange,
}: MicPickerProps) {
  const [open, setOpen] = useState(false);
  const [devices, setDevices] = useState<AudioDevice[] | null>(null);
  const [inventoryStatus, setInventoryStatus] =
    useState<InputDeviceInventoryStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Lazy-load devices on first expand so we don't hit the backend for users
  // who never open the picker.
  useEffect(() => {
    if (!open || inventoryStatus !== "idle") return;
    setInventoryStatus("loading");
    getDevices()
      .then((d) => {
        setDevices(d);
        setInventoryStatus("loaded");
        setError(null);
      })
      .catch((err) => {
        console.error(`[MicPicker] getDevices failed error_type=${errorType(err)}`);
        setInventoryStatus("error");
        setError("Couldn't load microphones.");
      });
  }, [open, inventoryStatus]);

  // Click-outside to close.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const currentDevice = selectedInputDevice(value, devices, selectionMode);
  const displayName = savedInputDeviceLabel(
    { audio_input_device: value, audio_input_device_label: savedLabel },
    currentDevice,
    selectionMode,
  );
  const availability = savedInputDeviceAvailability(
    value,
    currentDevice,
    selectionMode,
    inventoryStatus,
    savedAvailable,
  );

  return (
    <div ref={containerRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-[11px] text-steel/60 hover:text-steel transition-colors duration-150 cursor-pointer"
      >
        <motion.svg
          width="9"
          height="9"
          viewBox="0 0 12 12"
          fill="none"
          animate={{ rotate: open ? 90 : 0 }}
          transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
          className="shrink-0"
        >
          <path
            d="M4 2.5L8 6L4 9.5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </motion.svg>
        <span className="truncate max-w-[220px]">
          Microphone:&nbsp;<span className="text-steel/80">{displayName}</span>
          {availability === "unavailable" && (
            <span className="text-steel/60"> (Unavailable)</span>
          )}
          {availability === "unknown" && (
            <span className="text-steel/60"> (Availability unknown)</span>
          )}
        </span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.16, ease: [0.2, 0, 0, 1] }}
            className="absolute left-0 bottom-full mb-2 w-[260px] max-h-[160px] overflow-y-auto rounded-2xl py-1.5 z-10"
            style={{
              // Match Audio.tsx device dropdown — denser bg, simpler shadow.
              background: "var(--surface-overlay)",
              backdropFilter: "blur(24px)",
              border: "1px solid var(--border-emphasis)",
              boxShadow: "var(--shadow-glass-sm)",
            }}
          >
            <DeviceRow
              label="Default"
              selected={!value || value.trim() === ""}
              onClick={() => {
                onChange(inputDevicePatch(null, selectionMode));
                setOpen(false);
              }}
            />
            {inventoryStatus === "loading" && (
              <div className="px-3 py-1.5 text-[11px] text-steel/50 italic">
                Loading microphones…
              </div>
            )}
            {error && (
              <div className="px-3 py-1.5 text-[11px] text-rose">{error}</div>
            )}
            {devices?.map((d) => (
              <DeviceRow
                key={d.id}
                label={d.name}
                selected={value === inputDeviceValue(d, selectionMode)}
                onClick={() => {
                  onChange(inputDevicePatch(d, selectionMode));
                  setOpen(false);
                }}
              />
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function DeviceRow({
  label,
  selected,
  onClick,
}: {
  label: string;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-3 py-1.5 text-[11px] transition-colors duration-100 cursor-pointer flex items-center gap-2 ${
        selected ? "text-slate font-medium" : "text-steel hover:text-slate"
      }`}
      style={!selected ? { ["--hover-bg" as string]: "var(--surface-hover)" } : undefined}
      onMouseEnter={(e) => { if (!selected) (e.currentTarget as HTMLButtonElement).style.background = "var(--surface-hover)"; }}
      onMouseLeave={(e) => { if (!selected) (e.currentTarget as HTMLButtonElement).style.background = ""; }}
    >
      <span
        className="w-1 h-1 rounded-full shrink-0"
        style={{
          background: selected ? "var(--color-frost-deep)" : "transparent",
        }}
      />
      <span className="truncate">{label}</span>
    </button>
  );
}
