import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../../lib/api/config";
import type { EnhancementModel } from "../../lib/api/enhancement";
import type { TranscriptBakeoffSelection } from "../../lib/api/transcripts";
import type { AppEvent } from "../../lib/socket/events";
import type { TranscriptBakeoffMetadata, TranscriptBakeoffProgressEvent, TranscriptBakeoffResultEvent } from "../../lib/socket/bakeoffEvents";
import type { LockStatus } from "../../lib/useLockdown";
import { ExperimentalProvider } from "../../lib/experimental";
import {
  BAKEOFF_CARD_GAP_PX,
  BAKEOFF_CARD_MIN_PX,
  BAKEOFF_CARD_TARGET_PX,
  BAKEOFF_VIEWPORT_GUTTER_PX,
  BAKEOFF_WORKSPACE_PADDING_PX,
  bakeoffPreferredWidth,
  MAX_BAKEOFF_MODELS,
  MAX_BAKEOFF_PAIRS,
} from "../../components/BakeoffWorkspace";
import { TRANSCRIPT_DIFF_MAX_CHARS } from "../../lib/transcriptDiff";
import {
  INITIAL_BAKEOFF_SESSION_STATE,
  bakeoffSessionReducer,
} from "../transcripts/bakeoffSession";
import { Transcripts } from "../Transcripts";

const ITEMS = [
  {
    id: "saved-1",
    created_at: 1_750_000_000_000,
    duration_ms: 4200,
    has_audio: false,
    has_enhanced: false,
    snippet: "First original dictated words.",
  },
  {
    id: "saved-2",
    created_at: 1_749_000_000_000,
    duration_ms: 3600,
    has_audio: false,
    has_enhanced: false,
    snippet: "Second original dictated words.",
  },
];

const SELECTIONS: TranscriptBakeoffSelection[] = [
  { model_id: "stream-1", profile_id: "chat" },
  { model_id: "gemma4-e2b-instruct", profile_id: "email" },
];

const METADATA: TranscriptBakeoffMetadata = {
  serving_contract: "wrap-v2",
  device_mode: "cpu",
  resident_warm: false,
  load_ms: 12,
  inference_ms: 34,
  execution_mode: "whole",
  chunk_count: 1,
};

// The merged local catalog: production repository rows (installed, downloadable
// but absent, and one unpublished row) plus registered development candidates.
const CATALOG: EnhancementModel[] = [
  {
    id: "stream-1",
    label: "Stream-1",
    ram_mb: 1200,
    source: "production",
    artifact_available: true,
    selectable: true,
    downloadable: true,
    active: true,
  },
  {
    id: "stream-1-lite",
    // Production rows without a shared display name use their catalog label.
    label: "Stream-1 Lite",
    ram_mb: 640,
    source: "production",
    artifact_available: true,
    selectable: true,
    downloadable: true,
  },
  {
    id: "gemma4-e2b-instruct",
    label: "Gemma 4 E2B Instruct",
    ram_mb: 1600,
    source: "production",
    artifact_available: true,
    selectable: true,
    downloadable: true,
  },
  {
    id: "stream-2-preview",
    label: "Stream-2 Preview",
    ram_mb: 900,
    source: "production",
    artifact_available: false,
    selectable: true,
    downloadable: true,
  },
  {
    id: "unpublished-finetune",
    label: "Unpublished fine-tune",
    ram_mb: 700,
    source: "production",
    artifact_available: false,
    selectable: false,
    downloadable: true,
  },
  {
    id: "qwen35-0p8b-s1",
    label: "qwen35-0p8b-s1",
    experiment_id: "qwen35-0p8b-s1",
    ram_mb: 0,
    resident_ram_mb: 4352,
    resident_ram_estimated: true,
    source: "candidate",
    parameter_size: "0.8B",
    role: "Review candidate",
    status: "Ready",
    serving_contract: "chatml",
    trained_modes: ["chat"],
    artifact_available: true,
    selectable: true,
    downloadable: false,
  },
  {
    id: "w1wrap350m-s2",
    label: "w1wrap350m-s2",
    experiment_id: "w1wrap350m-s2",
    ram_mb: 0,
    resident_ram_mb: 1408,
    resident_ram_estimated: true,
    source: "candidate",
    parameter_size: "350M",
    role: "Ship pick",
    status: "Ready",
    serving_contract: "wrap-v2",
    trained_modes: ["chat", "code"],
    artifact_available: true,
    selectable: true,
    downloadable: false,
  },
  {
    id: "lfm2-1p2b-s4",
    label: "lfm2-1p2b-s4",
    experiment_id: "lfm2-1p2b-s4",
    ram_mb: 0,
    source: "candidate",
    parameter_size: "1.2B",
    role: "Review candidate",
    status: "Missing artifact",
    serving_contract: "raw-v1",
    trained_modes: ["chat", "code"],
    artifact_available: false,
    selectable: false,
    downloadable: false,
  },
];

interface BakeoffPost {
  status: number;
  body: unknown;
}

/** The one controlled input every comparison is diffed against. */
const RAW_TEXT = "okay so this is an update on the reenhance feature";
/** An answer that only appends, so its added run stays one contiguous span. */
const SURVIVING_ANSWER = `${RAW_TEXT} and a completed answer.`;

let bakeoffPost: BakeoffPost = {
  status: 200,
  body: { run_id: "run-9", generation: 3, total_count: 2 },
};
let cancelPost: BakeoffPost = {
  status: 200,
  body: { run_id: "run-9", cancelled: true },
};
let retryPost: BakeoffPost = {
  status: 202,
  body: { run_id: "run-9", generation: 4, pair_index: 1 },
};
let catalogRunning = false;
let catalogFails = false;
let recordFails = false;

function resultEvent({
  runId = "run-1",
  generation = 7,
  pairIndex = 0,
  output = "Ephemeral private result.",
}: {
  runId?: string;
  generation?: number;
  pairIndex?: number;
  output?: string;
} = {}): TranscriptBakeoffResultEvent {
  return {
    type: "enhancement.bakeoff.result",
    version: 1,
    run_id: runId,
    generation,
    pair_index: pairIndex,
    state: "complete",
    finished_count: 1,
    total_count: 2,
    output,
    metadata: METADATA,
  };
}

/** A live progress event for the run the workspace tests actually start. */
function liveProgress({
  pairIndex,
  state,
  finished,
  generation = 3,
  errorCode = null,
  metadata = METADATA,
}: {
  pairIndex: number;
  state: TranscriptBakeoffProgressEvent["state"];
  finished: number;
  generation?: number;
  errorCode?: string | null;
  metadata?: TranscriptBakeoffMetadata;
}): TranscriptBakeoffProgressEvent {
  return {
    type: "enhancement.bakeoff.progress",
    version: 1,
    run_id: "run-9",
    generation,
    pair_index: pairIndex,
    state,
    finished_count: finished,
    total_count: 2,
    error_code: errorCode,
    metadata,
  };
}

function liveResult({
  pairIndex,
  output,
  finished,
  generation = 3,
  state = "complete",
  metadata = METADATA,
}: {
  pairIndex: number;
  output: string;
  finished: number;
  generation?: number;
  state?: TranscriptBakeoffResultEvent["state"];
  metadata?: TranscriptBakeoffMetadata;
}): TranscriptBakeoffResultEvent {
  return {
    type: "enhancement.bakeoff.result",
    version: 1,
    run_id: "run-9",
    generation,
    pair_index: pairIndex,
    state,
    finished_count: finished,
    total_count: 2,
    output,
    metadata,
  };
}

function makeConfig(lockdownMode = false): AppConfig {
  return {
    history_enabled: true,
    save_audio: true,
    transcript_retention_days: 7,
    lockdown_mode: lockdownMode,
    enhancement_local_model: "stream-1",
    profiles: [
      { id: "chat", name: "Chat", prompt: "" },
      { id: "code", name: "Code", prompt: "" },
      { id: "email", name: "Email", prompt: "" },
    ],
  } as AppConfig;
}

function makeLockStatus({
  locked = false,
  enabled = locked,
}: {
  locked?: boolean;
  enabled?: boolean;
} = {}): LockStatus {
  return {
    locked,
    lockoutRemainingSeconds: 0,
    enabled,
    managed: false,
    hasEnvelope: enabled,
    historyEnabled: true,
    acknowledgedVersion: null,
    refresh: vi.fn(),
  };
}

function makeSubscription() {
  const handlers = new Set<(event: AppEvent) => void>();
  const unsubscribers: Array<ReturnType<typeof vi.fn>> = [];
  const subscribe = vi.fn((handler: (event: AppEvent) => void) => {
    handlers.add(handler);
    const unsubscribe = vi.fn(() => handlers.delete(handler));
    unsubscribers.push(unsubscribe);
    return unsubscribe;
  });
  const emit = (event: AppEvent) => {
    for (const handler of [...handlers]) handler(event);
  };
  return { subscribe, emit, handlers, unsubscribers };
}

function renderPage({
  config = makeConfig(),
  lockStatus = makeLockStatus(),
  subscription = makeSubscription(),
  onConfigChange = vi.fn(),
  experimental = true,
}: {
  config?: AppConfig;
  lockStatus?: LockStatus;
  subscription?: ReturnType<typeof makeSubscription>;
  onConfigChange?: ReturnType<typeof vi.fn>;
  experimental?: boolean;
} = {}) {
  const view = render(
    <ExperimentalProvider enabled={experimental}>
      <Transcripts
        config={config}
        onConfigChange={onConfigChange}
        subscribe={subscription.subscribe}
        lockStatus={lockStatus}
      />
    </ExperimentalProvider>,
  );
  return { ...view, subscription, onConfigChange };
}

/** Open the workspace from the first transcript's fire action. */
async function openWorkspace(options?: Parameters<typeof renderPage>[0]) {
  const view = renderPage(options);
  const triggers = await screen.findAllByRole("button", {
    name: "Open enhancement bakeoff",
  });
  fireEvent.click(triggers[0]);
  const dialog = await screen.findByRole("dialog");
  // The lineup seeds only once the merged catalog resolves.
  await screen.findByRole("group", { name: "Comparison 1" });
  return { ...view, dialog, triggers };
}

function modelTrigger(index: number): HTMLElement {
  return screen.getByRole("button", {
    name: new RegExp(`^Model for comparison ${index}:`),
  });
}

function profileTrigger(index: number): HTMLElement {
  return screen.getByRole("button", {
    name: new RegExp(`^Profile for comparison ${index}:`),
  });
}

async function chooseOption(trigger: HTMLElement, name: RegExp) {
  fireEvent.click(trigger);
  fireEvent.click(await screen.findByRole("option", { name }));
}

function addComparison() {
  fireEvent.click(screen.getByRole("button", { name: /Add comparison/ }));
}

/** Open the lineup, run the seeded Stream-1 Chat / Stream-1 Code pair, and
 *  wait for the shared Original well to resolve. */
async function startRun(options?: Parameters<typeof renderPage>[0]) {
  const view = await openWorkspace(options);
  fireEvent.click(screen.getByRole("button", { name: "Run bakeoff" }));
  await screen.findAllByText("0 of 2 finished");
  return view;
}

function emit(
  subscription: ReturnType<typeof makeSubscription>,
  ...events: AppEvent[]
) {
  act(() => {
    for (const event of events) subscription.emit(event);
  });
}

function answerCard(identity: string): HTMLElement {
  return screen.getByRole("region", { name: identity });
}

function diffSpans(op: "added" | "removed"): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>(`[data-diff-op="${op}"]`),
  );
}

beforeEach(() => {
  bakeoffPost = {
    status: 200,
    body: { run_id: "run-9", generation: 3, total_count: 2 },
  };
  cancelPost = { status: 200, body: { run_id: "run-9", cancelled: true } };
  retryPost = {
    status: 202,
    body: { run_id: "run-9", generation: 4, pair_index: 1 },
  };
  catalogRunning = false;
  catalogFails = false;
  recordFails = false;
  document.documentElement.removeAttribute("data-theme");
  Element.prototype.scrollIntoView = vi.fn();
  globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/transcripts?limit=20")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ items: ITEMS, next_cursor: null }),
      } as Response);
    }
    if (/\/api\/transcripts\/saved-\d+$/.test(url)) {
      if (recordFails) {
        return Promise.resolve({
          ok: false,
          status: 500,
          text: () => Promise.resolve("boom"),
          json: () => Promise.resolve({}),
        } as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            id: "saved-1",
            created_at: ITEMS[0].created_at,
            duration_ms: ITEMS[0].duration_ms,
            model: "parakeet",
            raw_text: RAW_TEXT,
            enhanced_text: null,
            peaks: null,
            has_audio: false,
          }),
      } as Response);
    }
    if (url.includes("/cancel") && init?.method === "POST") {
      return Promise.resolve({
        ok: cancelPost.status < 400,
        status: cancelPost.status,
        json: () => Promise.resolve(cancelPost.body),
      } as Response);
    }
    if (url.includes("/retry") && init?.method === "POST") {
      return Promise.resolve({
        ok: retryPost.status < 400,
        status: retryPost.status,
        json: () => Promise.resolve(retryPost.body),
      } as Response);
    }
    if (url.includes("/api/enhancement/models?provider=local")) {
      if (catalogFails) {
        return Promise.resolve({
          ok: false,
          status: 500,
          text: () => Promise.resolve("boom"),
          json: () => Promise.resolve({}),
        } as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            provider: "local",
            bundled: CATALOG,
            discovered: [],
            installed_ids: ["stream-1", "stream-1-lite", "gemma4-e2b-instruct"],
            available_ids: [
              "stream-1",
              "stream-1-lite",
              "gemma4-e2b-instruct",
              "stream-2-preview",
            ],
            active_id: "stream-1",
            running: catalogRunning,
          }),
      } as Response);
    }
    if (url.includes("/bakeoff") && init?.method === "POST") {
      return Promise.resolve({
        ok: bakeoffPost.status < 400,
        status: bakeoffPost.status,
        json: () => Promise.resolve(bakeoffPost.body),
      } as Response);
    }
    throw new Error(`Unexpected fetch: ${url}`);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Bakeoff session reducer", () => {
  it("preserves an authorized session across close and reopen, then clears it for another transcript", () => {
    let state = bakeoffSessionReducer(INITIAL_BAKEOFF_SESSION_STATE, {
      type: "open",
      transcriptId: "saved-1",
    });
    state = bakeoffSessionReducer(state, {
      type: "run.requested",
      selections: SELECTIONS,
    });
    const clientGeneration = state.clientGeneration;
    state = bakeoffSessionReducer(state, {
      type: "run.accepted",
      clientGeneration,
      result: { run_id: "run-1", generation: 7, total_count: 2 },
      selections: SELECTIONS,
    });
    state = bakeoffSessionReducer(state, {
      type: "event",
      event: resultEvent(),
    });

    state = bakeoffSessionReducer(state, { type: "close" });
    expect(state.session?.open).toBe(false);
    expect(state.session?.pairs[0].output).toBe("Ephemeral private result.");

    state = bakeoffSessionReducer(state, {
      type: "open",
      transcriptId: "saved-1",
    });
    expect(state.session?.open).toBe(true);
    expect(state.session?.pairs[0].output).toBe("Ephemeral private result.");

    state = bakeoffSessionReducer(state, {
      type: "open",
      transcriptId: "saved-2",
    });
    expect(state.clientGeneration).toBeGreaterThan(clientGeneration);
    expect(state.session).toMatchObject({
      transcriptId: "saved-2",
      phase: "configure",
      runId: null,
      pairs: [],
    });
    expect(JSON.stringify(state)).not.toContain("Ephemeral private result.");

    const afterTranscriptChange = state;
    state = bakeoffSessionReducer(state, {
      type: "run.accepted",
      clientGeneration,
      result: { run_id: "run-1", generation: 7, total_count: 2 },
      selections: SELECTIONS,
    });
    expect(state).toBe(afterTranscriptChange);
  });

  it("ignores stale run generations and clears matching revoked or reset sessions", () => {
    let state = bakeoffSessionReducer(INITIAL_BAKEOFF_SESSION_STATE, {
      type: "open",
      transcriptId: "saved-1",
    });
    state = bakeoffSessionReducer(state, {
      type: "run.requested",
      selections: SELECTIONS,
    });
    state = bakeoffSessionReducer(state, {
      type: "run.accepted",
      clientGeneration: state.clientGeneration,
      result: { run_id: "run-1", generation: 7, total_count: 2 },
      selections: SELECTIONS,
    });

    const beforeStale = state;
    state = bakeoffSessionReducer(state, {
      type: "event",
      event: resultEvent({ generation: 6, output: "Stale decrypted output." }),
    });
    expect(state).toBe(beforeStale);
    expect(JSON.stringify(state)).not.toContain("Stale decrypted output.");

    state = bakeoffSessionReducer(state, {
      type: "event",
      event: resultEvent(),
    });
    const afterTerminal = state;
    state = bakeoffSessionReducer(state, {
      type: "event",
      event: {
        type: "enhancement.bakeoff.progress",
        version: 1,
        run_id: "run-1",
        generation: 7,
        pair_index: 0,
        state: "running",
        finished_count: 1,
        total_count: 2,
        error_code: null,
        metadata: METADATA,
      },
    });
    expect(state).toBe(afterTerminal);

    state = bakeoffSessionReducer(state, {
      type: "event",
      event: {
        type: "enhancement.bakeoff.revoked",
        version: 1,
        run_id: "run-1",
        generation: 7,
        reason: "lockdown_locked",
      },
    });
    expect(state.session).toBeNull();

    state = bakeoffSessionReducer(state, {
      type: "open",
      transcriptId: "saved-1",
    });
    state = bakeoffSessionReducer(state, { type: "reset" });
    expect(state.session).toBeNull();
  });

  it("uses a new client generation for every run request", () => {
    let state = bakeoffSessionReducer(INITIAL_BAKEOFF_SESSION_STATE, {
      type: "open",
      transcriptId: "saved-1",
    });
    state = bakeoffSessionReducer(state, {
      type: "run.requested",
      selections: SELECTIONS,
    });
    const staleClientGeneration = state.clientGeneration;

    state = bakeoffSessionReducer(state, {
      type: "run.requested",
      selections: SELECTIONS,
    });
    const currentRequest = state;
    expect(state.clientGeneration).toBeGreaterThan(staleClientGeneration);

    state = bakeoffSessionReducer(state, {
      type: "run.accepted",
      clientGeneration: staleClientGeneration,
      result: { run_id: "stale-run", generation: 6, total_count: 2 },
      selections: SELECTIONS,
    });
    expect(state).toBe(currentRequest);
  });

  it("replays matching WebSocket progress received before start acceptance and rejects stale generations", () => {
    let state = bakeoffSessionReducer(INITIAL_BAKEOFF_SESSION_STATE, {
      type: "open",
      transcriptId: "saved-1",
    });
    state = bakeoffSessionReducer(state, {
      type: "run.requested",
      selections: SELECTIONS,
    });
    const clientGeneration = state.clientGeneration;

    state = bakeoffSessionReducer(state, {
      type: "event",
      event: {
        type: "enhancement.bakeoff.progress",
        version: 1,
        run_id: "run-early",
        generation: 7,
        pair_index: 1,
        state: "loading",
        finished_count: 0,
        total_count: 2,
        error_code: null,
        metadata: METADATA,
      },
    });
    state = bakeoffSessionReducer(state, {
      type: "event",
      event: resultEvent({
        runId: "run-early",
        generation: 7,
        output: "Early private result.",
      }),
    });
    state = bakeoffSessionReducer(state, {
      type: "event",
      event: resultEvent({
        runId: "run-early",
        generation: 6,
        output: "Stale private result.",
      }),
    });
    state = bakeoffSessionReducer(state, {
      type: "event",
      event: resultEvent({
        runId: "unrelated-run",
        generation: 7,
        output: "Unrelated private result.",
      }),
    });

    state = bakeoffSessionReducer(state, {
      type: "run.accepted",
      clientGeneration,
      result: { run_id: "run-early", generation: 7, total_count: 2 },
      selections: SELECTIONS,
    });

    expect(state.session?.pairs[0]).toMatchObject({
      state: "complete",
      output: "Early private result.",
    });
    expect(state.session?.pairs[1]).toMatchObject({
      state: "loading",
      output: null,
    });
    expect(state.session?.generation).toBe(7);
    expect(JSON.stringify(state)).not.toContain("Stale private result.");
    expect(JSON.stringify(state)).not.toContain("Unrelated private result.");
  });

  it("returns a refused start to the editable lineup and ignores stale refusals", () => {
    let state = bakeoffSessionReducer(INITIAL_BAKEOFF_SESSION_STATE, {
      type: "open",
      transcriptId: "saved-1",
    });
    state = bakeoffSessionReducer(state, {
      type: "run.requested",
      selections: SELECTIONS,
    });
    const requestGeneration = state.clientGeneration;

    const stale = bakeoffSessionReducer(state, {
      type: "run.rejected",
      clientGeneration: requestGeneration - 1,
    });
    expect(stale).toBe(state);

    state = bakeoffSessionReducer(state, {
      type: "run.rejected",
      clientGeneration: requestGeneration,
    });
    expect(state.session).toMatchObject({
      phase: "configure",
      runId: null,
      generation: null,
      totalCount: 0,
      pairs: [],
    });
    expect(state.clientGeneration).toBe(requestGeneration);

    // An accepted run can no longer be reverted by a late refusal.
    state = bakeoffSessionReducer(state, {
      type: "run.requested",
      selections: SELECTIONS,
    });
    state = bakeoffSessionReducer(state, {
      type: "run.accepted",
      clientGeneration: state.clientGeneration,
      result: { run_id: "run-2", generation: 4, total_count: 2 },
      selections: SELECTIONS,
    });
    const accepted = state;
    state = bakeoffSessionReducer(state, {
      type: "run.rejected",
      clientGeneration: state.clientGeneration,
    });
    expect(state).toBe(accepted);
  });
});

describe("Transcripts Bakeoff lifecycle ownership", () => {
  it("keeps cards to an identity-only trigger and restores its focus on close", async () => {
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const clipboardWrite = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    const { onConfigChange } = renderPage();
    const triggers = await screen.findAllByRole("button", {
      name: "Open enhancement bakeoff",
    });

    fireEvent.click(triggers[0]);
    expect(triggers[0]).toHaveAttribute("data-bakeoff-trigger-active", "true");
    expect(triggers[1]).not.toHaveAttribute("data-bakeoff-trigger-active");
    // The card emits identity only: the workspace itself is owned by the page.
    expect(await screen.findByRole("dialog")).toHaveAttribute(
      "aria-modal",
      "true",
    );
    expect(
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some(([url]) =>
        String(url).includes("/bakeoff"),
      ),
    ).toBe(false);
    expect(onConfigChange).not.toHaveBeenCalled();
    expect(clipboardWrite).not.toHaveBeenCalled();
    expect(storageWrite).not.toHaveBeenCalled();

    triggers[0].blur();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(triggers[0]).toHaveFocus());
    expect(triggers[0]).not.toHaveAttribute("data-bakeoff-trigger-active");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(triggers[0]);
    fireEvent.click(triggers[1]);
    expect(triggers[0]).not.toHaveAttribute("data-bakeoff-trigger-active");
    expect(triggers[1]).toHaveAttribute("data-bakeoff-trigger-active", "true");
  });

  it("revokes state for Lockdown and unsubscribes every page handler on unmount", async () => {
    const subscription = makeSubscription();
    const view = renderPage({ subscription });
    const trigger = (
      await screen.findAllByRole("button", { name: "Open enhancement bakeoff" })
    )[0];
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("data-bakeoff-trigger-active", "true");

    view.rerender(
      <ExperimentalProvider enabled>
        <Transcripts
          config={makeConfig(true)}
          onConfigChange={view.onConfigChange}
          subscribe={subscription.subscribe}
          lockStatus={makeLockStatus({ enabled: true })}
        />
      </ExperimentalProvider>,
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Open enhancement bakeoff" }),
      ).not.toBeInTheDocument();
    });
    // Gate-off DOM is structurally absent, not merely hidden.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    view.rerender(
      <ExperimentalProvider enabled>
        <Transcripts
          config={makeConfig(false)}
          onConfigChange={view.onConfigChange}
          subscribe={subscription.subscribe}
          lockStatus={makeLockStatus()}
        />
      </ExperimentalProvider>,
    );
    const reopenedTrigger = (
      await screen.findAllByRole("button", { name: "Open enhancement bakeoff" })
    )[0];
    expect(reopenedTrigger).not.toHaveAttribute("data-bakeoff-trigger-active");

    view.unmount();
    expect(subscription.handlers).toHaveLength(0);
    expect(subscription.unsubscribers.every((unsubscribe) =>
      unsubscribe.mock.calls.length === 1
    )).toBe(true);
  });

  it("reserves enough header room that four actions cannot reach the timestamp", async () => {
    renderPage();
    const trigger = (
      await screen.findAllByRole("button", { name: "Open enhancement bakeoff" })
    )[0];
    const card = trigger.closest("[data-transcript-card]") as HTMLElement;
    const rail = trigger.parentElement as HTMLElement;
    const actions = within(rail).getAllByRole("button");
    const meta = within(card)
      .getByRole("button", { name: /^Open transcript from/ })
      .firstElementChild as HTMLElement;

    // 24px actions on a 4px gap, inset by the rail's own right-3.
    const railWidth = actions.length * 24 + (actions.length - 1) * 4 + 12;
    const reserved = Number(
      /pr-(\d+)/.exec(meta?.className ?? "")?.[1] ?? 0,
    ) * 4;
    expect(actions).toHaveLength(4);
    expect(reserved).toBeGreaterThanOrEqual(railWidth);
  });

  it("keeps the whole surface absent outside an experimental build", async () => {
    renderPage({ experimental: false });
    await screen.findByText("First original dictated words.");
    expect(
      screen.queryByRole("button", { name: "Open enhancement bakeoff" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("Bakeoff lineup workspace", () => {
  it.each(["light", "dark"] as const)(
    "uses the shared frost-glass token contract in the %s theme",
    async (theme) => {
      document.documentElement.setAttribute("data-theme", theme);
      const { dialog } = await openWorkspace();
      const backdrop = dialog.previousElementSibling as HTMLElement;
      const run = within(dialog).getByRole("button", { name: "Run bakeoff" });

      expect(dialog.style.background).toBe("var(--surface-modal)");
      expect(dialog.style.border).toBe("1px solid var(--border-emphasis)");
      expect(dialog.style.boxShadow).toBe("var(--shadow-glass)");
      expect(backdrop.style.background).toBe("var(--backdrop-strong)");
      expect(run.style.background).toBe("var(--accent-fill)");
      expect(dialog).toHaveAttribute(
        "data-bakeoff-width",
        String(bakeoffPreferredWidth(2)),
      );
    },
  );

  it("opens the workspace with the approved lineup copy and a seeded pair of combinations", async () => {
    const { dialog } = await openWorkspace();

    expect(
      within(dialog).getByRole("heading", { name: "Compare enhancements" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Run the same raw transcript through each model and profile.",
      ),
    ).toBeInTheDocument();
    expect(dialog).toHaveAttribute("aria-modal", "true");

    // Two distinct combinations on the configured model.
    expect(modelTrigger(1)).toHaveAccessibleName(
      "Model for comparison 1: Stream-1",
    );
    expect(profileTrigger(1)).toHaveAccessibleName(
      "Profile for comparison 1: Chat",
    );
    expect(modelTrigger(2)).toHaveAccessibleName(
      "Model for comparison 2: Stream-1",
    );
    expect(profileTrigger(2)).toHaveAccessibleName(
      "Profile for comparison 2: Code",
    );

    // Footprint, residency, and device disclosure before Run.
    expect(
      within(dialog).getByText("1 model will use about 2.0 GB."),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Selected models stay loaded until you exit Whisperstream.",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Your active model keeps its current device. Additional models use CPU, so compare output quality rather than speed.",
      ),
    ).toBeInTheDocument();

    expect(
      within(dialog).getByRole("button", { name: "Run bakeoff" }),
    ).toBeEnabled();
    // Remove is held at the two-combination minimum.
    expect(
      within(dialog).getByRole("button", {
        name: "Remove comparison Stream-1, Chat",
      }),
    ).toBeDisabled();
  });

  it("lists the complete merged catalog and keeps unavailable rows visible but disabled", async () => {
    await openWorkspace();
    fireEvent.click(modelTrigger(1));

    const production = await screen.findByRole("option", {
      name: /^Customizable/,
    });
    expect(production).toBeEnabled();
    // Registered development candidates appear beside production rows.
    expect(
      screen.getByRole("option", { name: /^qwen35-0p8b-s1/ }),
    ).toBeEnabled();
    expect(screen.getByRole("option", { name: /0\.8B · Review candidate · Ready/ }))
      .toBeInTheDocument();

    const uninstalled = screen.getByRole("option", {
      name: /^Stream-2 Preview/,
    });
    expect(uninstalled).toBeDisabled();
    expect(
      within(uninstalled).getByText(
        "Download this model in Enhancement, then try again.",
      ),
    ).toBeInTheDocument();

    const missingCandidate = screen.getByRole("option", {
      name: /^lfm2-1p2b-s4/,
    });
    expect(missingCandidate).toBeDisabled();
    expect(
      within(missingCandidate).getByText(
        "This candidate's model file is unavailable. Choose another model.",
      ),
    ).toBeInTheDocument();

    // An unpublished production row stays hidden, exactly as in Enhancement.
    expect(
      screen.queryByRole("option", { name: /Unpublished fine-tune/ }),
    ).not.toBeInTheDocument();
  });

  it("blocks a combination the candidate was not trained for", async () => {
    await openWorkspace();
    // Comparison 2 seeds on the Code profile; this candidate trained chat only.
    await chooseOption(modelTrigger(2), /^qwen35-0p8b-s1/);

    const guidance =
      "qwen35-0p8b-s1 was not trained for the Code profile. Choose another profile.";
    const group = await screen.findByRole("group", { name: "Comparison 2" });
    await waitFor(() =>
      expect(group).toHaveAttribute("aria-describedby"),
    );
    const describedBy = group.getAttribute("aria-describedby") ?? "";
    expect(document.getElementById(describedBy)).toHaveTextContent(guidance);
    expect(screen.getByRole("button", { name: "Run bakeoff" })).toBeDisabled();

    // The same constraint disables the profile option itself.
    fireEvent.click(profileTrigger(2));
    expect(screen.getByRole("option", { name: /^Code/ })).toBeDisabled();
    fireEvent.click(await screen.findByRole("option", { name: /^Chat/ }));

    await waitFor(() =>
      expect(
        screen.getByRole("group", { name: "Comparison 2" }),
      ).not.toHaveAttribute("aria-describedby"),
    );
    expect(screen.getByRole("button", { name: "Run bakeoff" })).toBeEnabled();
  });

  it("requires two different combinations and reports a duplicate lineup", async () => {
    await openWorkspace();
    await chooseOption(profileTrigger(2), /^Chat/);

    expect(
      await screen.findByText(
        "Add at least two different model and profile combinations.",
      ),
    ).toBeInTheDocument();
    const run = screen.getByRole("button", { name: "Run bakeoff" });
    expect(run).toBeDisabled();
    expect(run).toHaveAttribute("aria-describedby");
  });

  it("holds the eight-comparison and four-model bounds", async () => {
    await openWorkspace();

    for (let i = 0; i < MAX_BAKEOFF_PAIRS - 2; i += 1) addComparison();
    expect(
      await screen.findByRole("group", { name: `Comparison ${MAX_BAKEOFF_PAIRS}` }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: `Comparison ${MAX_BAKEOFF_PAIRS + 1}` }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add comparison/ })).toBeDisabled();

    // Four unique models is the cap: a fifth model can no longer be selected.
    await chooseOption(modelTrigger(2), /^Stream-1 Lite/);
    await chooseOption(modelTrigger(3), /^Customizable/);
    await chooseOption(modelTrigger(4), /^qwen35-0p8b-s1/);
    fireEvent.click(modelTrigger(5));
    const fifth = await screen.findByRole("option", { name: /^w1wrap350m-s2/ });
    expect(fifth).toBeDisabled();
    expect(
      within(fifth).getByText(
        `A bakeoff can load up to ${MAX_BAKEOFF_MODELS} models.`,
      ),
    ).toBeInTheDocument();
    // A model already in the lineup stays selectable.
    expect(screen.getByRole("option", { name: /^Customizable/ })).toBeEnabled();
  }, 15_000);

  it("uses the backend artifact estimate for a multi-GB candidate and excludes the resident model", async () => {
    catalogRunning = true;
    await openWorkspace();
    await chooseOption(modelTrigger(2), /^qwen35-0p8b-s1/);
    await chooseOption(profileTrigger(2), /^Chat/);

    // stream-1 is the running production model, so only the candidate is new.
    expect(
      await screen.findByText("2 models will use about 4.3 GB."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Memory includes an estimate for qwen35-0p8b-s1."),
    ).toBeInTheDocument();
  });

  it("traps Tab inside the dialog and restores the trigger on Esc", async () => {
    const { dialog, triggers } = await openWorkspace();
    expect(dialog).toHaveFocus();

    const dismiss = within(dialog).getAllByRole("button", { name: "Close" })[0];
    const run = within(dialog).getByRole("button", { name: "Run bakeoff" });

    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(dismiss).toHaveFocus();

    fireEvent.keyDown(dismiss, { key: "Tab", shiftKey: true });
    expect(run).toHaveFocus();

    fireEvent.keyDown(run, { key: "Tab" });
    expect(dismiss).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(triggers[0]).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the lineup when the workspace is closed and reopened", async () => {
    const { triggers } = await openWorkspace();
    addComparison();
    await screen.findByRole("group", { name: "Comparison 3" });

    fireEvent.click(within(screen.getByRole("dialog")).getAllByRole("button", {
      name: "Close",
    })[1]);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );

    fireEvent.click(triggers[0]);
    expect(
      await screen.findByRole("group", { name: "Comparison 3" }),
    ).toBeInTheDocument();
  });

  it("grows toward the viewport as the lineup grows, with a 12px gutter", async () => {
    const { dialog } = await openWorkspace();
    expect(dialog).toHaveAttribute(
      "data-bakeoff-width",
      String(bakeoffPreferredWidth(2)),
    );
    expect(dialog.style.maxWidth).toBe("calc(100vw - 24px)");
    expect(dialog.style.maxHeight).toBe("calc(100vh - 24px)");

    addComparison();
    addComparison();
    await screen.findByRole("group", { name: "Comparison 4" });
    expect(dialog).toHaveAttribute(
      "data-bakeoff-width",
      String(bakeoffPreferredWidth(4)),
    );
    expect(bakeoffPreferredWidth(4)).toBeGreaterThan(bakeoffPreferredWidth(2));
    // Six 380px cards still fit inside a 2560px display's available width.
    expect(bakeoffPreferredWidth(6)).toBeLessThanOrEqual(2560 - 24);
  });

  it("starts one run from the lineup and honors the backend's busy refusal", async () => {
    bakeoffPost = { status: 409, body: { error: "evaluation_busy" } };
    await openWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Run bakeoff" }));

    expect(
      await screen.findByText(
        "Finish the current recording or enhancement, then try again.",
      ),
    ).toBeInTheDocument();
    const post = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([url]) => String(url).includes("/bakeoff"),
    );
    expect(String(post?.[0])).toContain(
      "/api/experimental/transcripts/saved-1/bakeoff",
    );
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({
      selections: [
        { model_id: "stream-1", profile_id: "chat" },
        { model_id: "stream-1", profile_id: "code" },
      ],
    });
    // The refused lineup stays editable.
    expect(screen.getByRole("group", { name: "Comparison 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run bakeoff" })).toBeEnabled();

    bakeoffPost = {
      status: 200,
      body: { run_id: "run-9", generation: 3, total_count: 2 },
    };
    fireEvent.click(screen.getByRole("button", { name: "Run bakeoff" }));
    // Header progress plus the polite live-region summary.
    expect(await screen.findAllByText("0 of 2 finished")).toHaveLength(2);
    expect(screen.getAllByText("Queued")).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: "Run bakeoff" }),
    ).not.toBeInTheDocument();
  });

  it("explains a catalog it could not load instead of guessing model ids", async () => {
    catalogFails = true;
    renderPage();
    const trigger = (
      await screen.findAllByRole("button", { name: "Open enhancement bakeoff" })
    )[0];
    fireEvent.click(trigger);
    expect(
      await screen.findByText("Models could not be loaded. Close this and try again."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Comparison 1" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run bakeoff" })).toBeDisabled();
  });
});

describe("Bakeoff comparison rail", () => {
  it("keeps every lineup slot a side-by-side peer card in request order", async () => {
    const { subscription } = await startRun();

    const rail = document.querySelector<HTMLElement>('[data-bakeoff-rail="true"]');
    expect(rail).not.toBeNull();
    // One horizontal rail: answers are never reflowed into a vertical list.
    expect(rail?.className).toContain("flex");
    expect(rail?.className).toContain("overflow-x-auto");
    expect(rail?.className).not.toContain("flex-col");
    expect(rail?.style.scrollSnapType).toBe("x proximity");
    // A completed card owns no control, so the rail itself must be reachable.
    expect(rail).toHaveAttribute("tabindex", "0");
    expect(rail).toHaveAccessibleName("Comparison results");

    const cards = Array.from(
      rail?.querySelectorAll<HTMLElement>("[data-bakeoff-card]") ?? [],
    );
    expect(cards.map((card) => card.getAttribute("aria-label"))).toEqual([
      "Stream-1, Chat",
      "Stream-1, Code",
    ]);
    for (const card of cards) {
      expect(card.style.width).toBe(`${BAKEOFF_CARD_TARGET_PX}px`);
      expect(card.style.minWidth).toBe(`${BAKEOFF_CARD_MIN_PX}px`);
      expect(card.style.scrollSnapAlign).toBe("start");
    }
    expect(screen.getAllByText("Queued")).toHaveLength(2);

    // Honest per-pair states, and honest N/M. No percentage, no ETA.
    emit(subscription, liveProgress({ pairIndex: 0, state: "loading", finished: 0 }));
    expect(within(answerCard("Stream-1, Chat")).getByText("Loading model"))
      .toBeInTheDocument();
    emit(subscription, liveProgress({ pairIndex: 0, state: "running", finished: 0 }));
    expect(
      within(answerCard("Stream-1, Chat")).getByText("Running enhancement"),
    ).toBeInTheDocument();
    expect(screen.getByText("0 of 2 finished")).toBeInTheDocument();

    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "This is an update.", finished: 1 }),
    );
    expect(within(answerCard("Stream-1, Chat")).getByText("Complete"))
      .toBeInTheDocument();
    expect(screen.getByText("1 of 2 finished")).toBeInTheDocument();
    // Lineup order is stable: the finished lane did not jump ahead.
    expect(
      Array.from(
        rail?.querySelectorAll<HTMLElement>("[data-bakeoff-card]") ?? [],
      ).map((card) => card.getAttribute("aria-label")),
    ).toEqual(["Stream-1, Chat", "Stream-1, Code"]);
  });

  it("shows two complete cards at app width and fills a 1440p viewport before overflowing", async () => {
    // The rail's own geometry, resolved the way the browser resolves it: the
    // shell grows with the lineup up to the viewport, then the fixed-width
    // cards decide how many are fully visible. jsdom has no layout, so this
    // asserts the arithmetic the styles are built from.
    const visibleCards = (viewportWidth: number, lineup: number) => {
      const shell = Math.min(
        viewportWidth - BAKEOFF_VIEWPORT_GUTTER_PX,
        bakeoffPreferredWidth(lineup),
      );
      const inner = shell - BAKEOFF_WORKSPACE_PADDING_PX;
      return Math.floor(
        (inner + BAKEOFF_CARD_GAP_PX) /
          (BAKEOFF_CARD_TARGET_PX + BAKEOFF_CARD_GAP_PX),
      );
    };

    // 957px is the packaged Tauri main window width.
    expect(visibleCards(957, 2)).toBe(2);
    expect(visibleCards(957, 6)).toBe(2);
    // A 2560x1440 display reveals the whole six-card lineup at once.
    expect(visibleCards(2560, 6)).toBe(6);
    expect(visibleCards(2560, MAX_BAKEOFF_PAIRS)).toBeGreaterThanOrEqual(6);
    // Card geometry is never traded away to fit one more answer.
    expect(BAKEOFF_CARD_MIN_PX).toBeLessThanOrEqual(BAKEOFF_CARD_TARGET_PX);

    const { subscription } = await startRun();
    emit(subscription, liveProgress({ pairIndex: 0, state: "loading", finished: 0 }));
    expect(screen.getByRole("dialog")).toHaveAttribute(
      "data-bakeoff-width",
      String(bakeoffPreferredWidth(2)),
    );
  });

  it("shows the shared original once and reports execution facts under identity", async () => {
    const { subscription, dialog } = await startRun();

    const original = await screen.findByRole("region", {
      name: "Original transcript",
    });
    expect(original).toHaveTextContent(RAW_TEXT);
    expect(screen.getAllByText(RAW_TEXT)).toHaveLength(1);

    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "This is an update.", finished: 1 }),
    );
    const card = answerCard("Stream-1, Chat");
    expect(
      within(card).getByText(
        "wrap-v2 · CPU · Cold · Load 12 ms · Whole · 1 chunk · Enhance 34 ms",
      ),
    ).toBeInTheDocument();
    expect(
      within(card).getByText(
        "Serving contract wrap-v2. Device CPU. Cold start. Model load 12 ms. Whole. 1 chunk. Enhancement 34 ms.",
      ),
    ).toBeInTheDocument();
    // Load and inference time are separate facts, never one number.
    expect(within(card).queryByText(/46 ms/)).not.toBeInTheDocument();

    // CPU and GPU lanes are never presented as directly comparable.
    expect(
      within(dialog).getByText(
        "Your active model keeps its current device. Additional models use CPU, so compare output quality rather than speed.",
      ),
    ).toBeInTheDocument();

    // The original is collapsible and stays one compact well.
    fireEvent.click(screen.getByRole("button", { name: "Original" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("region", { name: "Original transcript" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("defaults to Diff and switches every completed card to Clean together", async () => {
    const { subscription, dialog } = await startRun();
    emit(
      subscription,
      liveResult({
        pairIndex: 0,
        output: "This is an update on the Re-enhance feature.",
        finished: 1,
      }),
      liveResult({
        pairIndex: 1,
        output: "okay so this is an update on the reenhance feature.",
        finished: 2,
      }),
    );

    // Diff is the default for the whole workspace.
    expect(
      within(dialog).getByRole("button", { name: "Diff" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(diffSpans("added").length).toBeGreaterThan(0);
    expect(diffSpans("removed").length).toBeGreaterThan(0);
    // Mint additions and rose removals only; no third inline change color.
    expect(diffSpans("added")[0].style.textDecorationLine).toBe("underline");
    expect(diffSpans("removed")[0].style.textDecorationLine).toBe("line-through");
    expect(diffSpans("added")[0].tagName).toBe("INS");
    expect(diffSpans("removed")[0].tagName).toBe("DEL");
    expect(diffSpans("added")[0]).toHaveAccessibleName(/^Added:/);
    expect(diffSpans("removed")[0]).toHaveAccessibleName(/^Removed:/);
    expect(document.querySelectorAll("[data-diff-op]").length).toBe(
      diffSpans("added").length + diffSpans("removed").length,
    );

    // Non-color cues: the legend plus per-card counts.
    expect(within(dialog).getByText("Added")).toBeInTheDocument();
    expect(within(dialog).getByText("Removed")).toBeInTheDocument();
    const first = answerCard("Stream-1, Chat");
    expect(within(first).getByText(/^\+\d+$/)).toBeInTheDocument();
    expect(within(first).getByText(/^−\d+$/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Clean" }));
    await waitFor(() =>
      expect(document.querySelectorAll("[data-diff-op]")).toHaveLength(0),
    );
    // One control, every card: both answers are now plain text.
    expect(
      within(answerCard("Stream-1, Chat")).getByText(
        "This is an update on the Re-enhance feature.",
      ),
    ).toBeInTheDocument();
    expect(
      within(answerCard("Stream-1, Code")).getByText(
        "okay so this is an update on the reenhance feature.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText("Added")).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Diff" }));
    await waitFor(() =>
      expect(diffSpans("added").length).toBeGreaterThan(0),
    );
  });

  it("falls back to clean text when a result is too long to highlight safely", async () => {
    const { subscription } = await startRun();
    const huge = "word ".repeat(TRANSCRIPT_DIFF_MAX_CHARS / 4);
    emit(subscription, liveResult({ pairIndex: 0, output: huge, finished: 1 }));

    const card = answerCard("Stream-1, Chat");
    expect(
      within(card).getByText(
        "This result is too long to highlight safely. Showing clean text instead.",
      ),
    ).toBeInTheDocument();
    expect(diffSpans("added")).toHaveLength(0);
    expect(diffSpans("removed")).toHaveLength(0);
  });

  it("explains an unchanged result instead of showing an empty diff", async () => {
    const { subscription } = await startRun();
    emit(
      subscription,
      liveResult({
        pairIndex: 0,
        output: RAW_TEXT,
        finished: 1,
        state: "unchanged",
      }),
    );

    const card = answerCard("Stream-1, Chat");
    expect(within(card).getByText("Unchanged")).toBeInTheDocument();
    expect(
      within(card).getByText(
        "The model returned the original, or a safeguard kept it unchanged.",
      ),
    ).toBeInTheDocument();
    expect(within(card).queryByText(/^\+\d+$/)).not.toBeInTheDocument();
  });

  it("keeps a completed peer after a failure and retries only the failed lane", async () => {
    const { subscription } = await startRun();
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: SURVIVING_ANSWER, finished: 1 }),
      liveProgress({
        pairIndex: 1,
        state: "failed",
        finished: 2,
        errorCode: "model_load_failed",
      }),
    );

    // Partial failure never removes a completed result. The diff splits the
    // answer across spans, so assert on the card's text content.
    expect(answerCard("Stream-1, Chat")).toHaveTextContent(
      "and a completed answer.",
    );
    const failedCard = answerCard("Stream-1, Code");
    expect(within(failedCard).getByText("Failed")).toBeInTheDocument();
    expect(
      within(failedCard).getByText(
        "Stream-1 could not load. Remove it or retry this result.",
      ),
    ).toBeInTheDocument();
    expect(failedCard).toHaveAttribute("aria-describedby");
    expect(screen.getByText("1 of 2 results completed.")).toBeInTheDocument();

    const retry = within(failedCard).getByRole("button", {
      name: "Retry this result Stream-1, Code",
    });
    await act(async () => {
      fireEvent.click(retry);
    });
    const post = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([url]) => String(url).includes("/retry"),
    );
    expect(String(post?.[0])).toContain(
      "/api/experimental/bakeoffs/run-9/retry",
    );
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({
      pair_index: 1,
    });

    // Retry allocates a new server generation, so the retried lane walks back
    // to queued while its completed peer is untouched.
    emit(
      subscription,
      liveProgress({
        pairIndex: 1,
        state: "queued",
        finished: 1,
        generation: 4,
        metadata: {
          serving_contract: null,
          device_mode: null,
          resident_warm: null,
          load_ms: null,
          inference_ms: null,
          execution_mode: null,
          chunk_count: null,
        },
      }),
    );
    expect(
      within(answerCard("Stream-1, Code")).getByText("Queued"),
    ).toBeInTheDocument();
    expect(answerCard("Stream-1, Chat")).toHaveTextContent(
      "and a completed answer.",
    );

    emit(
      subscription,
      liveResult({
        pairIndex: 1,
        output: "A second update.",
        finished: 2,
        generation: 4,
      }),
    );
    expect(screen.getByText("2 of 2 results completed.")).toBeInTheDocument();
  });

  it("reports a refused retry beside its own lane", async () => {
    retryPost = { status: 409, body: { error: "evaluation_busy" } };
    const { subscription } = await startRun();
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "This is an update.", finished: 1 }),
      liveProgress({
        pairIndex: 1,
        state: "failed",
        finished: 2,
        errorCode: "evaluation_failed",
      }),
    );

    const failedCard = answerCard("Stream-1, Code");
    expect(
      within(failedCard).getByText(
        "Stream-1 could not finish this result. Retry when the app is idle.",
      ),
    ).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(
        within(failedCard).getByRole("button", {
          name: "Retry this result Stream-1, Code",
        }),
      );
    });
    expect(
      within(answerCard("Stream-1, Code")).getByText(
        "Finish the current recording or enhancement, then try again.",
      ),
    ).toBeInTheDocument();
  });

  it("cancels the remaining lanes without deleting completed results", async () => {
    const { subscription, dialog } = await startRun();
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: SURVIVING_ANSWER, finished: 1 }),
      liveProgress({ pairIndex: 1, state: "running", finished: 1 }),
    );

    await act(async () => {
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Cancel remaining" }),
      );
    });
    const post = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      ([url]) => String(url).includes("/cancel"),
    );
    expect(String(post?.[0])).toContain(
      "/api/experimental/bakeoffs/run-9/cancel",
    );

    emit(
      subscription,
      liveProgress({
        pairIndex: 1,
        state: "cancelled",
        finished: 2,
        generation: 4,
      }),
    );
    expect(
      within(answerCard("Stream-1, Code")).getByText("Cancelled"),
    ).toBeInTheDocument();
    expect(answerCard("Stream-1, Chat")).toHaveTextContent(
      "and a completed answer.",
    );
    expect(screen.getByText("1 of 2 results completed.")).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "Cancel remaining" }),
    ).not.toBeInTheDocument();
  });

  it("returns to the lineup on Edit lineup and repeats it on Run again", async () => {
    const { subscription, dialog } = await startRun();
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "First answer.", finished: 1 }),
      liveResult({ pairIndex: 1, output: "Second answer.", finished: 2 }),
    );

    // Run again repeats the same lineup without reopening the editor.
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "Run again" }));
    });
    const runs = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.filter(
      ([url]) => String(url).endsWith("/bakeoff"),
    );
    expect(runs).toHaveLength(2);
    expect(JSON.parse(String((runs[1][1] as RequestInit).body))).toEqual({
      selections: [
        { model_id: "stream-1", profile_id: "chat" },
        { model_id: "stream-1", profile_id: "code" },
      ],
    });
    expect(screen.getAllByText("Queued")).toHaveLength(2);
    expect(screen.queryByText("First answer.")).not.toBeInTheDocument();

    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "First answer.", finished: 1 }),
      liveResult({ pairIndex: 1, output: "Second answer.", finished: 2 }),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Edit lineup" }));

    expect(
      await screen.findByRole("group", { name: "Comparison 1" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("First answer.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run bakeoff" })).toBeEnabled();
  });

  it("preserves results across close and reopen and offers no ranking or copy", async () => {
    const { subscription, dialog, triggers } = await startRun();
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "First answer.", finished: 1 }),
      liveResult({ pairIndex: 1, output: "Second answer.", finished: 2 }),
    );

    // Neutral peers: no score, rank, winner, or result copy control.
    expect(
      within(dialog).queryByRole("button", { name: /copy/i }),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByText(/winner|best|score|rank/i),
    ).not.toBeInTheDocument();

    fireEvent.click(
      within(dialog).getAllByRole("button", { name: "Close" })[1],
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("First answer.")).not.toBeInTheDocument();

    fireEvent.click(triggers[0]);
    expect(await screen.findByText("First answer.")).toBeInTheDocument();
    expect(screen.getByText("2 of 2 results completed.")).toBeInTheDocument();
  });

  it("announces pair identity and state politely without elapsed-time chatter", async () => {
    const { subscription, dialog } = await startRun();
    const live = dialog.querySelector<HTMLElement>('[aria-live="polite"]');
    expect(live).toHaveTextContent("0 of 2 finished");

    emit(subscription, liveProgress({ pairIndex: 0, state: "running", finished: 0 }));
    expect(live).toHaveTextContent(
      "Stream-1, Chat. Running enhancement. 0 of 2 finished",
    );

    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "This is an update.", finished: 1 }),
    );
    expect(live).toHaveTextContent("Stream-1, Chat. Complete. 1 of 2 finished");

    emit(
      subscription,
      liveProgress({
        pairIndex: 1,
        state: "failed",
        finished: 2,
        errorCode: "evaluation_failed",
      }),
    );
    expect(live).toHaveTextContent(
      "Stream-1, Code. Failed. 1 of 2 results completed.",
    );
  });

  it("ignores a stale result and drops every decrypted answer under Lockdown", async () => {
    const subscription = makeSubscription();
    const view = await startRun({ subscription });
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "Authorized answer.", finished: 1 }),
    );
    expect(screen.getByText("Authorized answer.")).toBeInTheDocument();

    // An older generation for the same run cannot repopulate the lane.
    emit(
      subscription,
      liveResult({
        pairIndex: 0,
        output: "Stale decrypted output.",
        finished: 1,
        generation: 2,
      }),
    );
    expect(screen.queryByText("Stale decrypted output.")).not.toBeInTheDocument();
    expect(screen.getByText("Authorized answer.")).toBeInTheDocument();

    view.rerender(
      <ExperimentalProvider enabled>
        <Transcripts
          config={makeConfig(true)}
          onConfigChange={view.onConfigChange}
          subscribe={subscription.subscribe}
          lockStatus={makeLockStatus({ enabled: true })}
        />
      </ExperimentalProvider>,
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("Authorized answer.")).not.toBeInTheDocument();

    // A late event after revocation cannot bring the text back.
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "Authorized answer.", finished: 1 }),
    );
    expect(screen.queryByText("Authorized answer.")).not.toBeInTheDocument();
  });

  it("still compares when the original transcript cannot be read", async () => {
    recordFails = true;
    const { subscription, dialog } = await startRun();
    emit(
      subscription,
      liveResult({ pairIndex: 0, output: "This is an update.", finished: 1 }),
    );

    // No original means nothing to diff against, so the answer stays clean
    // rather than being highlighted against a guess.
    await waitFor(() =>
      expect(
        screen.queryByRole("region", { name: "Original transcript" }),
      ).not.toBeInTheDocument(),
    );
    expect(answerCard("Stream-1, Chat")).toHaveTextContent(
      "This is an update.",
    );
    expect(document.querySelectorAll("[data-diff-op]")).toHaveLength(0);
    expect(within(dialog).queryByText("Added")).not.toBeInTheDocument();
  });
});
