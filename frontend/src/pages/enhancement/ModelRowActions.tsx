import { useEffect, useRef } from "react";
import { ModelDeleteConfirmation, useModelDeleteConfirmation } from "./ModelDeleteConfirmation";
import { DownloadMiniIcon } from "./Glyphs";
import { HoverTooltip } from "../../components/HoverTooltip";


/* Per-model row action for the local model picker's `renderOptionAction`
 * (design.md Components §3 "Row action"). ONE 24px icon button in the
 * Dropdown's absolute right slot:
 *  - missing model: download, visible at rest (steel/70) and lit frost-deep on
 *    row hover; starts the download in place without changing the selection.
 *  - installed model: trash, revealed on row hover or keyboard focus
 *    (`:focus-visible`, so a mouse-opened panel's focused row stays quiet), with the
 *    inline `Delete?` confirm. Any installed model (including a too-slow,
 *    disabled row) is deletable from its own row.
 *  - downloading: no action.
 * The picker enables `optionActionsFocusable`, so Tab moves from a row into its
 * action. Esc inside the confirm cancels and returns focus to the trash. */
export function ModelRowActions({
  installed,
  downloading = false,
  modelId,
  modelLabel,
  sizeLabel,
  onDelete,
  onDownload,
}: {
  installed: boolean;
  downloading?: boolean;
  modelId: string;
  /** Display label used in the accessible names ("Download Stream-1 Mini, 462
   *  MB", "Delete Stream-1 Mini"); a bare "Download local model" is ambiguous
   *  with two rows (copy-review.md Accessibility Notes). */
  modelLabel: string;
  /** Compact download size ("462 MB"). */
  sizeLabel: string;
  onDelete: (modelId: string) => void;
  onDownload: () => void;
}) {
  const { deletePending, setDeletePending, confirmBtnRef } = useModelDeleteConfirmation(installed);
  const trashRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);

  useEffect(() => {
    if (!deletePending && returnFocusRef.current) {
      returnFocusRef.current = false;
      trashRef.current?.focus();
    }
  }, [deletePending]);

  if (downloading) return null;

  if (!installed) {
    return (
      <HoverTooltip
        content={`Download · ${sizeLabel}`}
        side="auto"
        width="w-auto whitespace-nowrap"
        focusVisibleClassName="group-has-[:focus-visible]:opacity-100"
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDownload();
          }}
          aria-label={`Download ${modelLabel}, ${sizeLabel}`}
          className="w-6 h-6 rounded-full flex items-center justify-center text-steel/70 hover:text-frost-deep group-hover/row:text-frost-deep focus-visible:text-frost-deep transition-colors cursor-pointer"
        >
          <DownloadMiniIcon aria-hidden="true" />
        </button>
      </HoverTooltip>
    );
  }

  if (deletePending) {
    const cancelAndReturn = (pending: boolean) => {
      if (!pending) returnFocusRef.current = true;
      setDeletePending(pending);
    };
    return (
      <div
        onKeyDown={(e) => {
          if (e.key !== "Escape") return;
          // Esc in the confirm cancels it; it must not also close the panel.
          e.preventDefault();
          e.stopPropagation();
          cancelAndReturn(false);
        }}
      >
        <ModelDeleteConfirmation
          confirmBtnRef={confirmBtnRef}
          setDeletePending={cancelAndReturn}
          onDelete={(id) => {
            returnFocusRef.current = false;
            onDelete(id);
          }}
          modelId={modelId}
          modelLabel={modelLabel}
        />
      </div>
    );
  }

  return (
    <HoverTooltip
      content="Frees up space. You can download it again anytime."
      side="auto"
      width="w-48"
      focusVisibleClassName="group-has-[:focus-visible]:opacity-100"
    >
      <button
        ref={trashRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setDeletePending(true);
        }}
        aria-label={`Delete ${modelLabel}`}
        className="w-6 h-6 rounded-full flex items-center justify-center text-steel/70 hover:text-rose hover:bg-[var(--surface-hover)] opacity-0 group-hover/row:opacity-100 group-has-[:focus-visible]/row:opacity-100 focus-visible:opacity-100 transition-[opacity,color,background-color] cursor-pointer"
      >
        <svg
          viewBox="0 0 12 12"
          width="12"
          height="12"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M2 3.5h8" />
          <path d="M3 3.5v6.5a1 1 0 001 1h4a1 1 0 001-1V3.5" />
          <path d="M5 3.5v-1a1 1 0 011-1h0a1 1 0 011 1v1" />
          <path d="M5 5.5v3M7 5.5v3" />
        </svg>
      </button>
    </HoverTooltip>
  );
}
