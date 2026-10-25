import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";
import { ExperimentalProvider } from "../../lib/experimental";
import { Transcripts } from "../Transcripts";

const ENHANCED_TEXT = "Enhanced heading\n\n- First complete point\n- Second complete point";
const RAW_TEXT = "Original heading\n\nFirst raw paragraph.\nSecond raw paragraph.";

type TestProvenance = unknown;

const SUCCESS_PROVENANCE = {
  profile_id: "code",
  profile_name: "Code",
  outcome: "success",
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

function makeItem(
  hasEnhanced: boolean,
  enhancementProvenance: TestProvenance = null,
) {
  return {
    id: "saved-1",
    created_at: 1_750_000_000_000,
    duration_ms: 4200,
    has_audio: false,
    has_enhanced: hasEnhanced,
    enhancement_provenance: enhancementProvenance,
    snippet: hasEnhanced
      ? "Enhanced heading - First complete point"
      : "Original heading First raw paragraph.",
  };
}

function makeRecord(
  hasEnhanced: boolean,
  enhancementProvenance: TestProvenance = null,
) {
  return {
    ...makeItem(hasEnhanced, enhancementProvenance),
    model: "parakeet",
    raw_text: RAW_TEXT,
    enhanced_text: hasEnhanced ? ENHANCED_TEXT : null,
    peaks: null,
  };
}

function makeFetch(
  hasEnhanced: boolean,
  enhancementProvenance: TestProvenance = null,
  detailProvenance: TestProvenance = enhancementProvenance,
) {
  const item = makeItem(hasEnhanced, enhancementProvenance);
  const record = makeRecord(hasEnhanced, detailProvenance);
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/transcripts/saved-1")) {
      return Promise.resolve(jsonResponse(record));
    }
    if (url.includes("/api/transcripts")) {
      return Promise.resolve(
        jsonResponse({ items: [item], next_cursor: null }),
      );
    }
    throw new Error(`Unexpected fetch: ${url}`);
  });
}

const config = {
  history_enabled: true,
  save_audio: true,
  transcript_retention_days: 7,
  lockdown_mode: false,
} as AppConfig;

const subscribe = (_handler: (event: AppEvent) => void) => () => {};

function getProvenanceTooltips() {
  return screen.queryAllByRole("tooltip").filter((tooltip) =>
    /^(Profile:|Enhancement |Some of this transcript )/.test(
      tooltip.textContent ?? "",
    ),
  );
}

function getDescribedTooltip(control: HTMLElement) {
  const tooltipId = control.getAttribute("aria-describedby");
  expect(tooltipId).toBeTruthy();
  const tooltip = document.getElementById(tooltipId ?? "");
  expect(tooltip).toHaveAttribute("role", "tooltip");
  return tooltip as HTMLElement;
}

function renderPage(
  hasEnhanced: boolean,
  enhancementProvenance: TestProvenance = null,
  detailProvenance: TestProvenance = enhancementProvenance,
) {
  globalThis.fetch = makeFetch(
    hasEnhanced,
    enhancementProvenance,
    detailProvenance,
  );
  return render(
    <ExperimentalProvider enabled={false}>
      <Transcripts
        config={config}
        onConfigChange={vi.fn()}
        subscribe={subscribe}
      />
    </ExperimentalProvider>,
  );
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Transcripts version chips and Copy", () => {
  it("describes a successful collapsed enhancement from the existing header control", async () => {
    const view = renderPage(true, SUCCESS_PROVENANCE);

    const header = await screen.findByRole("button", {
      name: /Open transcript from/,
    });
    const chip = view.container.querySelector(
      '[data-transcript-version="enhanced"]',
    );
    const tooltip = getDescribedTooltip(header);

    expect(chip).toHaveTextContent("Enhanced");
    expect(chip).not.toHaveAttribute("tabindex");
    expect(tooltip).toHaveTextContent("Profile: Code");
    expect(tooltip).toHaveClass("w-64");
    expect(tooltip.className).toContain(
      "group-focus-visible/transcript-header:opacity-100",
    );
    expect(document.getElementById(tooltip.id)).toBe(tooltip);
  });

  it.each([
    [
      "timeout",
      "Enhancement took too long, so the original transcript was used.",
    ],
    [
      "error",
      "Enhancement couldn’t finish, so the original transcript was used.",
    ],
  ])(
    "shows a subtle collapsed Original with friendly %s fallback copy",
    async (outcome, explanation) => {
      const view = renderPage(false, {
        profile_id: "code",
        profile_name: "Code",
        outcome,
      });

      const header = await screen.findByRole("button", {
        name: /Open transcript from/,
      });
      const chip = view.container.querySelector(
        '[data-transcript-version="original"]',
      );
      const tooltip = getDescribedTooltip(header);

      expect(chip).toHaveTextContent("Original");
      expect(chip).toHaveClass("text-steel/70");
      expect(chip).not.toHaveAttribute("tabindex");
      expect(tooltip).toHaveTextContent(explanation);
      expect(tooltip).not.toHaveTextContent("Profile:");

      fireEvent.click(header);
      const originalWell = await screen.findByRole("region", {
        name: "Transcript (Original)",
      });
      const expandedTooltip = getDescribedTooltip(originalWell);
      expect(expandedTooltip).toHaveTextContent(explanation);
      expect(
        screen.queryByRole("button", { name: "Show enhanced" }),
      ).toBeNull();
    },
  );

  it.each([
    [
      "timeout",
      "Some of this transcript took too long to enhance, so the original wording was kept.",
    ],
    [
      "error",
      "Some of this transcript couldn’t be enhanced, so the original wording was kept.",
    ],
  ])(
    "describes a partial %s result from the expanded well and toggle",
    async (outcome, explanation) => {
      const longProfileName = `Code ${"planning notes ".repeat(12)}`.trim();
      const provenance = {
        profile_id: "custom-code",
        profile_name: longProfileName,
        outcome,
      };
      const view = renderPage(true, provenance);

      fireEvent.click(
        await screen.findByRole("button", { name: /Open transcript from/ }),
      );
      const well = await screen.findByRole("region", {
        name: "Transcript (Enhanced)",
      });
      const toggle = screen.getByRole("button", { name: "Show original" });
      const tooltip = getDescribedTooltip(well);

      expect(
        view.container.querySelector('[data-transcript-version="enhanced"]'),
      ).toHaveTextContent("Enhanced");
      expect(tooltip).toHaveTextContent(`Profile: ${longProfileName}`);
      expect(tooltip).toHaveTextContent(explanation);
      expect(tooltip.querySelector(".break-words")).toBeInTheDocument();
      expect(tooltip.className).toContain(
        "group-focus-within/transcript-detail:opacity-100",
      );
      expect(well).toHaveAttribute("aria-describedby", tooltip.id);
      expect(toggle).toHaveAttribute("aria-describedby", tooltip.id);
    },
  );

  it("removes partial-fallback explanation when the user deliberately shows Original", async () => {
    const provenance = {
      profile_id: "code",
      profile_name: "Code",
      outcome: "error",
    };
    const view = renderPage(true, provenance);

    fireEvent.click(
      await screen.findByRole("button", { name: /Open transcript from/ }),
    );
    const enhancedWell = await screen.findByRole("region", {
      name: "Transcript (Enhanced)",
    });
    expect(enhancedWell).toHaveAttribute("aria-describedby");

    fireEvent.click(screen.getByRole("button", { name: "Show original" }));
    const originalWell = await screen.findByRole("region", {
      name: "Transcript (Original)",
    });
    expect(originalWell).not.toHaveAttribute("aria-describedby");
    expect(
      screen.getByRole("button", { name: "Show enhanced" }),
    ).not.toHaveAttribute("aria-describedby");
    expect(getProvenanceTooltips()).toHaveLength(0);
    expect(
      view.container.querySelector('[data-transcript-version="original"]'),
    ).toHaveTextContent("Original");
  });

  it.each([
    ["unknown outcome", { ...SUCCESS_PROVENANCE, outcome: "cancelled" }],
    ["missing profile name", { profile_id: "code", outcome: "error" }],
    ["extra raw detail", { ...SUCCESS_PROVENANCE, raw_error: "private" }],
  ])("fails closed for malformed provenance with %s", async (_label, provenance) => {
    const view = renderPage(false, provenance);

    const header = await screen.findByRole("button", {
      name: /Open transcript from/,
    });
    expect(view.container.querySelector("[data-transcript-version]")).toBeNull();
    expect(getProvenanceTooltips()).toHaveLength(0);
    expect(header).not.toHaveAttribute("aria-describedby");
  });

  it("fails closed for contradictory success without enhanced text", async () => {
    const view = renderPage(false, SUCCESS_PROVENANCE);

    await screen.findByRole("button", { name: /Open transcript from/ });
    expect(view.container.querySelector("[data-transcript-version]")).toBeNull();
    expect(getProvenanceTooltips()).toHaveLength(0);
  });

  it("treats malformed enhanced provenance as a legacy Enhanced row", async () => {
    const view = renderPage(true, {
      ...SUCCESS_PROVENANCE,
      outcome: "future-outcome",
    });

    const header = await screen.findByRole("button", {
      name: /Open transcript from/,
    });
    expect(
      view.container.querySelector('[data-transcript-version="enhanced"]'),
    ).toHaveTextContent("Enhanced");
    expect(getProvenanceTooltips()).toHaveLength(0);
    expect(header).not.toHaveAttribute("aria-describedby");
  });

  it("labels an enhanced collapsed card and copies its complete formatted text without expanding", async () => {
    const view = renderPage(true);

    await screen.findByRole("button", { name: /Open transcript from/ });
    expect(
      view.container.querySelector('[data-transcript-version="enhanced"]'),
    ).toHaveTextContent("Enhanced");
    expect(
      view.container.querySelector(
        '[data-transcript-version="enhanced"] [data-transcript-sparkle]',
      ),
    ).toBeInTheDocument();
    expect(
      view.container.querySelectorAll(
        '[data-transcript-version="enhanced"] [data-transcript-sparkle] path',
      ),
    ).toHaveLength(2);
    fireEvent.click(
      screen.getByRole("button", { name: "Copy enhanced transcript" }),
    );

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(ENHANCED_TEXT);
    });
    expect(
      screen.queryByRole("button", { name: /Collapse transcript from/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Transcript (Enhanced)" }),
    ).not.toBeInTheDocument();
  });

  it("leaves original-only collapsed cards unadorned and keeps expanded chips and Copy names version-specific", async () => {
    const view = renderPage(false);

    await screen.findByRole("button", { name: /Open transcript from/ });
    expect(
      view.container.querySelector("[data-transcript-version]"),
    ).toBeNull();
    expect(getProvenanceTooltips()).toHaveLength(0);
    fireEvent.click(
      screen.getByRole("button", { name: "Copy original transcript" }),
    );
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(RAW_TEXT);
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Open transcript from/ }),
    );
    await screen.findByRole("region", { name: "Transcript (Original)" });
    expect(
      view.container.querySelector('[data-transcript-version="original"]'),
    ).toHaveTextContent("Original");
    expect(getProvenanceTooltips()).toHaveLength(0);

    view.unmount();
    const enhancedView = renderPage(true);
    fireEvent.click(
      await screen.findByRole("button", { name: /Open transcript from/ }),
    );
    await screen.findByRole("region", { name: "Transcript (Enhanced)" });
    expect(
      enhancedView.container.querySelector(
        '[data-transcript-version="enhanced"]',
      ),
    ).toHaveTextContent("Enhanced");
    expect(getProvenanceTooltips()).toHaveLength(0);
    expect(
      screen.getByRole("button", { name: "Copy enhanced transcript" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show original" }));
    await screen.findByRole("region", { name: "Transcript (Original)" });
    expect(
      screen.getByText("Original", { exact: true }),
    ).toHaveAttribute("data-transcript-version", "original");
    expect(
      screen
        .getByText("Original", { exact: true })
        .querySelector("[data-transcript-sparkle]"),
    ).toBeNull();
    const copyOriginal = screen.getByRole("button", {
      name: "Copy original transcript",
    });
    fireEvent.click(copyOriginal);
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenLastCalledWith(RAW_TEXT);
    });
  });
});
