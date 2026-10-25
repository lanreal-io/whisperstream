/**
 * RecoverySetupGate: the Transcripts tab for existing history users without a
 * recovery key. The list, search, import and settings stay hidden until a key
 * exists; saving continues meanwhile, shown in the mint status line.
 *
 * Same in-tab composition as RecoveryUnlockScreen (gold key tile, centered
 * column). Copy is copy-review.md section 6.
 */

import { motion } from "framer-motion";
import { KeyGlyph } from "./RecoveryUnlockScreen";
import { placeFor, type HistoryPlatform } from "../lib/historyRecoveryCopy";

export interface RecoverySetupGateProps {
  platform: HistoryPlatform;
  /** Saved rows; the status line is omitted when the count is unavailable. */
  recordCount: number | null;
  onSetUp: () => void;
}

export function RecoverySetupGate({ platform, recordCount, onSetUp }: RecoverySetupGateProps) {
  const place = placeFor(platform);
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ type: "tween", duration: 0.15, ease: [0.2, 0, 0, 1] }}
      className="absolute inset-0 z-10 flex flex-col items-center justify-center px-8"
    >
      <div className="flex flex-col items-center w-full max-w-[340px] text-center">
        <div
          className="w-[88px] h-[88px] rounded-[var(--radius-card)] flex items-center justify-center mb-5"
          style={{
            background: "var(--surface-raised)",
            border: "1px solid var(--border-hairline)",
            boxShadow: "var(--shadow-glass-sm)",
            color: "var(--color-gold-deep)",
          }}
        >
          <KeyGlyph />
        </div>
        <h2 className="text-[18px] font-medium text-slate mb-1">Finish recovery setup</h2>
        <p className="text-[12px] text-steel mb-5 leading-relaxed">
          {`Add a recovery key to see your transcripts. Without one, they can't be restored if ${place} loses its encryption key.`}
        </p>
        <button
          type="button"
          onClick={onSetUp}
          className="px-6 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer"
          style={{ background: "var(--accent-fill)", boxShadow: "var(--shadow-pearl)" }}
        >
          Set up recovery key
        </button>
        {recordCount !== null && (
          <span className="inline-flex items-center gap-1.5 mt-4 text-[11px] text-steel">
            <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full" style={{ background: "var(--color-mint)" }} />
            {recordCount === 1
              ? "Saving is on. 1 transcript saved."
              : `Saving is on. ${recordCount} transcripts saved.`}
          </span>
        )}
      </div>
    </motion.div>
  );
}
