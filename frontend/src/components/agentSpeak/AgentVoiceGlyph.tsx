import { createContext, useContext, useId, useLayoutEffect, useState } from "react";
import { useReducedMotion } from "framer-motion";
import logoUrl from "../../assets/logo.png";

export const AgentVoiceCompletion = createContext(false);


/** The main polished logo is revealed by a continuous rounded SVG brush mask.
 * The same image stays in place after drawing, so neither material nor color swaps. */
export function AgentVoiceGlyph({ speaking, size = 28, provider }: { speaking: boolean; size?: number; provider?: string }) {
  const completed = useContext(AgentVoiceCompletion);
  const reduced = useReducedMotion();
  const [drawn, setDrawn] = useState(false);
  useLayoutEffect(() => { setDrawn(false); }, [speaking, completed]);
  const id = useId().replace(/:/g, "");
  return <svg width={size} height={size} viewBox="0 0 56.1 56.1" aria-hidden="true" data-agent-mark={provider} className="shrink-0" data-agent-glyph={speaking ? "speaking" : completed ? "completed" : "rest"}>
    {speaking ? <g fill="var(--frost-muted)">
      {[10, 19, 28, 19, 10].map((height, index) => <rect key={index} x={13 + index * 6.4} y={(56.1 - height) / 2} width="4.5" height={height} rx="2.25" style={{ transformOrigin: `${15.25 + index * 6.4}px 28.05px`, animation: reduced ? undefined : `agents-voice-wave .9s ${index * .11}s ease-in-out infinite` }} />)}
    </g> : <g key={completed ? "complete" : "rest"} transform="translate(2.64 11.775) scale(.105)">
      <defs><mask id={id} maskUnits="userSpaceOnUse" x="0" y="0" width="484" height="310">
        <path d="M48 36 C142 37 151 260 245 261 C335 261 334 149 450 149 C335 149 334 36 245 36 C151 36 142 261 48 262"
          onAnimationEnd={() => setDrawn(true)}
          fill="none" stroke="white" strokeWidth="82" strokeLinecap="round" strokeLinejoin="round" pathLength="1"
          style={{ strokeDasharray: 1, strokeDashoffset: 0, animation: completed && !reduced ? "agents-logo-draw .65s .22s both ease-in-out" : undefined }} />
      </mask></defs>
      <image href={logoUrl} x="0" y="0" width="484" height="310" mask={completed && !reduced && !drawn ? `url(#${id})` : undefined}
        style={{ animation: completed && reduced ? "agents-logo-fade .12s ease-out" : undefined }} />
    </g>}
  </svg>;
}
