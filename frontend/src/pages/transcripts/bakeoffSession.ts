import type { TranscriptBakeoffSelection, TranscriptBakeoffStartResult } from "../../lib/api/transcripts";
import type { TranscriptBakeoffEvent, TranscriptBakeoffMetadata } from "../../lib/socket/bakeoffEvents";

export interface BakeoffStartFailure {
  code: string | null;
  requiredMb?: number | null;
  availableMb?: number | null;
}

export type BakeoffSessionPhase = "configure" | "running" | "results";
export type BakeoffPairState =
  | "queued"
  | "loading"
  | "running"
  | "complete"
  | "unchanged"
  | "failed"
  | "cancelled";

export interface BakeoffPairSession {
  selection: TranscriptBakeoffSelection;
  state: BakeoffPairState;
  output: string | null;
  errorCode: string | null;
  metadata: TranscriptBakeoffMetadata | null;
}

export interface BakeoffSession {
  transcriptId: string;
  open: boolean;
  phase: BakeoffSessionPhase;
  runId: string | null;
  generation: number | null;
  finishedCount: number;
  totalCount: number;
  pairs: BakeoffPairSession[];
}

export interface BakeoffSessionState {
  /** Frontend-only authority epoch. It invalidates delayed API responses even
   * when they arrive before a server run ID can be correlated. */
  clientGeneration: number;
  session: BakeoffSession | null;
  /** Bounded handshake buffer for WS events that can beat the HTTP 202
   * response carrying their run ID. It is cleared on every authority change. */
  pendingEvents: TranscriptBakeoffEvent[];
}

export type BakeoffSessionAction =
  | { type: "open"; transcriptId: string }
  | { type: "close" }
  | { type: "reset" }
  | { type: "edit" }
  | { type: "run.requested"; selections: TranscriptBakeoffSelection[] }
  | {
      type: "run.accepted";
      clientGeneration: number;
      result: TranscriptBakeoffStartResult;
      selections: TranscriptBakeoffSelection[];
    }
  | { type: "run.rejected"; clientGeneration: number }
  | { type: "event"; event: TranscriptBakeoffEvent };

export const INITIAL_BAKEOFF_SESSION_STATE: BakeoffSessionState = {
  clientGeneration: 0,
  session: null,
  pendingEvents: [],
};

const MAX_PENDING_BAKEOFF_EVENTS = 64;

const TERMINAL_BAKEOFF_PAIR_STATES = new Set<BakeoffPairState>([
  "complete",
  "unchanged",
  "failed",
  "cancelled",
]);

const BAKEOFF_PAIR_STATE_ORDER: Record<BakeoffPairState, number> = {
  queued: 0,
  loading: 1,
  running: 2,
  complete: 3,
  unchanged: 3,
  failed: 3,
  cancelled: 3,
};

function newBakeoffSession(transcriptId: string): BakeoffSession {
  return {
    transcriptId,
    open: true,
    phase: "configure",
    runId: null,
    generation: null,
    finishedCount: 0,
    totalCount: 0,
    pairs: [],
  };
}

/** Pure, memory-only owner for one transcript's Bakeoff state. Content-bearing
 * events must match both the opaque run ID and server generation. */
export function bakeoffSessionReducer(
  state: BakeoffSessionState,
  action: BakeoffSessionAction,
): BakeoffSessionState {
  if (action.type === "open") {
    if (!action.transcriptId) return state;
    if (state.session?.transcriptId === action.transcriptId) {
      if (state.session.open) return state;
      return { ...state, session: { ...state.session, open: true } };
    }
    return {
      clientGeneration: state.clientGeneration + 1,
      session: newBakeoffSession(action.transcriptId),
      pendingEvents: [],
    };
  }

  if (action.type === "close") {
    if (!state.session?.open) return state;
    return { ...state, session: { ...state.session, open: false } };
  }

  if (action.type === "reset") {
    if (!state.session) return state;
    return {
      clientGeneration: state.clientGeneration + 1,
      session: null,
      pendingEvents: [],
    };
  }

  if (action.type === "edit") {
    // Edit lineup returns a finished comparison to the editable lineup. Every
    // decrypted result is dropped with it, and the authority epoch advances so
    // nothing from the abandoned run can be applied afterwards.
    if (!state.session || state.session.phase !== "results") return state;
    return {
      clientGeneration: state.clientGeneration + 1,
      pendingEvents: [],
      session: {
        ...state.session,
        phase: "configure",
        runId: null,
        generation: null,
        finishedCount: 0,
        totalCount: 0,
        pairs: [],
      },
    };
  }

  if (action.type === "run.requested") {
    if (
      !state.session?.open ||
      action.selections.length < 2 ||
      action.selections.length > 8
    ) {
      return state;
    }
    return {
      clientGeneration: state.clientGeneration + 1,
      pendingEvents: [],
      session: {
        ...state.session,
        phase: "running",
        runId: null,
        generation: null,
        finishedCount: 0,
        totalCount: action.selections.length,
        pairs: action.selections.map((selection) => ({
          selection: { ...selection },
          state: "queued",
          output: null,
          errorCode: null,
          metadata: null,
        })),
      },
    };
  }

  if (action.type === "run.accepted") {
    const { result, selections } = action;
    if (
      !state.session ||
      action.clientGeneration !== state.clientGeneration ||
      !result.run_id ||
      !Number.isInteger(result.generation) ||
      result.generation < 1 ||
      !Number.isInteger(result.total_count) ||
      result.total_count !== selections.length ||
      result.total_count !== state.session.totalCount ||
      state.session.pairs.length !== selections.length ||
      !state.session.pairs.every(
        (pair, index) =>
          pair.selection.model_id === selections[index].model_id &&
          pair.selection.profile_id === selections[index].profile_id,
      )
    ) {
      return state;
    }
    if (state.session.runId !== null || state.session.generation !== null) {
      return state;
    }
    const accepted: BakeoffSessionState = {
      ...state,
      pendingEvents: [],
      session: {
        ...state.session,
        runId: result.run_id,
        generation: result.generation,
      },
    };
    // Dispatch order cannot make this handshake lossy: exact run/generation
    // checks below replay only events authorized by the accepted response.
    return state.pendingEvents.reduce(
      (current, event) =>
        bakeoffSessionReducer(current, { type: "event", event }),
      accepted,
    );
  }

  if (action.type === "run.rejected") {
    // A refused start never became a server run, so the lineup returns to
    // configure with no queued pairs. The authority epoch is NOT bumped: the
    // matching check below is what already invalidated the failed request.
    if (
      !state.session ||
      action.clientGeneration !== state.clientGeneration ||
      state.session.phase !== "running" ||
      state.session.runId !== null
    ) {
      return state;
    }
    return {
      ...state,
      pendingEvents: [],
      session: {
        ...state.session,
        phase: "configure",
        runId: null,
        generation: null,
        finishedCount: 0,
        totalCount: 0,
        pairs: [],
      },
    };
  }

  const session = state.session;
  const event = action.event;
  if (
    session?.phase === "running" &&
    session.runId === null &&
    session.generation === null
  ) {
    const pendingEvents = [
      ...state.pendingEvents.slice(-(MAX_PENDING_BAKEOFF_EVENTS - 1)),
      event,
    ];
    return { ...state, pendingEvents };
  }
  // Generation is monotonic per run: `enhancement_bakeoff.cancel()` and
  // `.retry()` both allocate a NEW one, and the server's own authority ledger
  // (`server._on_bakeoff_progress`) adopts a higher generation for the same
  // run the same way. Strictly older generations stay rejected, which is what
  // keeps a stale result from repopulating a superseded lane.
  if (
    !session ||
    session.runId === null ||
    session.generation === null ||
    session.runId !== event.run_id ||
    event.generation < session.generation
  ) {
    return state;
  }
  const regenerated = event.generation > session.generation;
  if (event.type === "enhancement.bakeoff.revoked") {
    return {
      clientGeneration: state.clientGeneration + 1,
      session: null,
      pendingEvents: [],
    };
  }
  if (event.type === "enhancement.bakeoff.started") {
    return state;
  }
  if (
    event.total_count !== session.totalCount ||
    event.total_count !== session.pairs.length ||
    event.pair_index >= session.pairs.length ||
    (!regenerated && event.finished_count < session.finishedCount)
  ) {
    return state;
  }

  // A regenerated run is authoritative over its own lanes: cancellation makes
  // an in-flight lane terminal, and a retry legitimately walks one terminal
  // lane back to queued.
  const currentPair = session.pairs[event.pair_index];
  if (
    !regenerated &&
    (TERMINAL_BAKEOFF_PAIR_STATES.has(currentPair.state) ||
      BAKEOFF_PAIR_STATE_ORDER[event.state] <
        BAKEOFF_PAIR_STATE_ORDER[currentPair.state])
  ) {
    return state;
  }

  const pairs = session.pairs.map((pair, index): BakeoffPairSession => {
    if (index !== event.pair_index) return pair;
    if (event.type === "enhancement.bakeoff.result") {
      return {
        ...pair,
        state: event.state,
        output: event.output,
        errorCode: null,
        metadata: { ...event.metadata },
      };
    }
    return {
      ...pair,
      state: event.state,
      output: null,
      errorCode: event.error_code,
      metadata: { ...event.metadata },
    };
  });
  const terminal = pairs.every((pair) =>
    TERMINAL_BAKEOFF_PAIR_STATES.has(pair.state),
  );
  return {
    ...state,
    session: {
      ...session,
      phase: terminal ? "results" : "running",
      generation: event.generation,
      finishedCount: event.finished_count,
      pairs,
    },
  };
}
