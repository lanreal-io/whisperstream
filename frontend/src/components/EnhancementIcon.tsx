import type { SVGProps } from "react";

/** Shared Enhancement mark used anywhere the UI refers to enhanced text. */
export function EnhancementIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <path d="M6 1.5 L7 5 L10.5 6 L7 7 L6 10.5 L5 7 L1.5 6 L5 5 Z" />
      <path d="M12 9.5 L12.5 11.5 L14.5 12 L12.5 12.5 L12 14.5 L11.5 12.5 L9.5 12 L11.5 11.5 Z" />
    </svg>
  );
}
