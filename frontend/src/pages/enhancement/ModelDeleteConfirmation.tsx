import { useEffect, useRef, useState } from "react";

/** Row owners retain eligibility and reset dependencies; this owns only confirmation. */
export function useModelDeleteConfirmation(installed: boolean, provider?: string) {
  const [deletePending, setDeletePending] = useState(false);
  const confirmBtnRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (deletePending) confirmBtnRef.current?.focus();
  }, [deletePending]);
  useEffect(() => {
    setDeletePending(false);
  }, [provider, installed]);
  return { deletePending, setDeletePending, confirmBtnRef };
}

export function ModelDeleteConfirmation({ confirmBtnRef, setDeletePending, onDelete, modelId, modelLabel }: {
  confirmBtnRef: React.RefObject<HTMLButtonElement | null>;
  setDeletePending: (pending: boolean) => void;
  onDelete: (modelId: string) => void;
  modelId: string;
  /** Names the model in the confirm/cancel accessible labels ("Delete
   *  Stream-1 Mini", "Keep Stream-1 Mini"). Omitted = the generic labels. */
  modelLabel?: string;
}) {
  return (
      <div
        className="flex items-center gap-1.5 shrink-0"
        onClick={(e) => e.stopPropagation()}
      >
        <span
          className="text-[11px] font-medium"
          style={{ color: "var(--color-rose)" }}
        >
          Delete?
        </span>
        <button
          ref={confirmBtnRef}
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setDeletePending(false);
            onDelete(modelId);
          }}
          aria-label={modelLabel ? `Delete ${modelLabel}` : "Confirm delete local model"}
          className="w-6 h-6 rounded-full flex items-center justify-center transition-colors cursor-pointer"
          style={{
            background: "rgba(196, 139, 139, 0.15)",
            border: "1px solid rgba(196, 139, 139, 0.35)",
          }}
        >
          <svg
            viewBox="0 0 12 12"
            width="10"
            height="10"
            fill="none"
            stroke="var(--color-rose)"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M2.5 6.5l2.5 2.5 4.5-5" />
          </svg>
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setDeletePending(false);
          }}
          aria-label={modelLabel ? `Keep ${modelLabel}` : "Cancel delete"}
          className="w-6 h-6 rounded-full flex items-center justify-center text-steel hover:text-slate cursor-pointer"
        >
          <svg
            viewBox="0 0 12 12"
            width="10"
            height="10"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M3 3l6 6M9 3l-6 6" />
          </svg>
        </button>
      </div>
  );
}
