import { useState, useRef, useEffect } from "react";


/* ---------- API-key field: saved-fingerprint chip + paste input ----------
   Shared by every cloud provider (gemini / openai / anthropic / custom). Empty or
   editing state is a password input; once a key is saved the field collapses to a
   read-only masked fingerprint (computed SERVER-SIDE, so the full secret never
   reaches the browser) that the user clicks to drop back into editing and paste a
   new key. There is intentionally NO reveal toggle: the plaintext key never leaves
   the backend (privacy-centric posture). "Remove key" stays an explicit action in
   the row below (it also disables enhancement). */
export function ApiKeyField({
  value,
  storedKey,
  fingerprint,
  provider,
  inputBase,
  inputBaseStyle,
  onChange,
  onBlur,
  onFocus,
  onBlurCapture,
}: {
  value: string;
  storedKey: string;
  fingerprint: string;
  provider: string;
  inputBase: string;
  inputBaseStyle: React.CSSProperties;
  onChange: (value: string) => void;
  onBlur: () => void;
  onFocus: (e: React.FocusEvent<HTMLElement>) => void;
  onBlurCapture: (e: React.FocusEvent<HTMLElement>) => void;
}) {
  const [editing, setEditing] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Each provider has its own key, so reset the view when the provider changes.
  useEffect(() => {
    setEditing(false);
  }, [provider]);

  // Focus + select the input on entering editing so clicking the saved chip
  // drops straight into "paste a new key".
  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const hasSavedKey = storedKey.trim().length > 0;
  const showChip = hasSavedKey && !editing;

  if (showChip) {
    // Read-only chip: the server-computed fingerprint, clickable to replace.
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        aria-label="Replace API key"
        className={inputBase + " flex items-center text-left cursor-pointer"}
        style={inputBaseStyle}
        onFocus={onFocus}
        onBlur={onBlurCapture}
      >
        <span className="font-mono text-[13px] tracking-wide text-slate truncate">
          {fingerprint || "•".repeat(8)}
        </span>
      </button>
    );
  }

  return (
    <input
      ref={inputRef}
      type="password"
      value={value}
      onChange={(e) => {
        // Any keystroke means we're editing, so the field stays an input until
        // blur even after the debounced save lands a key into storedKey.
        setEditing(true);
        onChange(e.target.value);
      }}
      onBlur={() => {
        onBlur();
        setEditing(false);
      }}
      placeholder="Paste API key"
      aria-label="API key"
      className={inputBase}
      style={inputBaseStyle}
      onFocus={onFocus}
      onBlurCapture={onBlurCapture}
    />
  );
}
