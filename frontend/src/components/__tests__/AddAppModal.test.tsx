/**
 * Vitest + Testing Library coverage for AddAppModal (round 04: no "Route to:"
 * chooser; multi-select pending list; batch onAdd).
 *
 * Tests cover:
 *   - Renders nothing when open=false.
 *   - App mode (default): heading, subtitle, RunningAppList wrapper.
 *   - App|Website toggle uses radiogroup/radio + aria-checked semantics.
 *   - Switching to Website mode shows the domain input and hint text.
 *   - Switching back to App mode hides the domain input.
 *   - No "Route to:" profile chooser is rendered.
 *   - Add (footer) is disabled until at least one rule is staged.
 *   - Picking two apps then Add fires onAdd ONCE with both rules + initial profile.
 *   - A picked app disappears from the list.
 *   - Adding two domains stages both and Add fires onAdd with both site rules.
 *   - Manual app entry stages an app rule (does not commit immediately).
 *   - The footer Add label reflects the pending count ("Add rule" / "Add N rules").
 *   - Removing a pending chip drops it from the batch.
 *   - Cancel / Esc discard the pending list and fire onClose.
 *   - Pending list + mode reset when the modal re-opens.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within } from "@testing-library/react";
import { AddAppModal } from "../AddAppModal";
import type { Profile } from "../../lib/profiles";
import type { RunningApp } from "../../lib/api/apps";
import type { AdvancedCapabilityDecision } from "../../lib/advancedCapabilities";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const PROFILES: Profile[] = [
  { id: "chat", name: "Chat", prompt: "" },
  { id: "code", name: "Code", prompt: "" },
];

const RUNNING_APPS: RunningApp[] = [
  { process_name: "chrome.exe", display_name: "Chrome", icon: null },
  { process_name: "slack.exe", display_name: "Slack", icon: null },
];

const AVAILABLE: AdvancedCapabilityDecision = {
  enabled: true,
  explained: false,
  reason: "implemented",
  state: "available",
  presentation: "enable",
};
const UNAVAILABLE: AdvancedCapabilityDecision = {
  enabled: false,
  explained: false,
  reason: "unsupported-platform",
  state: "unavailable",
  presentation: "disable",
};

function makeProps(
  overrides: Partial<Parameters<typeof AddAppModal>[0]> = {},
) {
  return {
    open: true,
    profiles: PROFILES,
    initialProfileId: "chat",
    runningApps: RUNNING_APPS,
    appCapability: AVAILABLE,
    websiteCapability: AVAILABLE,
    pathBrowseCapability: AVAILABLE,
    loading: false,
    mappedProcessNames: new Set<string>(),
    onAdd: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Render guard
// ---------------------------------------------------------------------------

describe("AddAppModal — render guard", () => {
  it("renders nothing when open=false", () => {
    render(<AddAppModal {...makeProps({ open: false })} />);
    expect(screen.queryByText("Add a rule")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// App mode (default)
// ---------------------------------------------------------------------------

describe("AddAppModal — App mode (default)", () => {
  it("shows the locked heading", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(screen.getByText("Add a rule")).toBeInTheDocument();
  });

  it("shows the subtitle (no longer references choosing a profile)", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(
      screen.getByText(
        "Pick the apps and websites this profile cleans dictation for.",
      ),
    ).toBeInTheDocument();
  });

  it("does NOT render a 'Route to:' profile chooser", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(screen.queryByText("Route to:")).toBeNull();
  });

  it("shows both toggle pills with correct labels", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(screen.getByRole("radio", { name: "App" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Website" })).toBeInTheDocument();
  });

  it("App pill is aria-checked=true by default", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(screen.getByRole("radio", { name: "App" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("toggle track has role=radiogroup", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(screen.getByRole("radiogroup")).toBeInTheDocument();
  });

  it("does NOT show the domain input in App mode", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(screen.queryByRole("textbox", { name: "Website domain" })).toBeNull();
  });

  it("footer Add is disabled when nothing is staged", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Mode switching
// ---------------------------------------------------------------------------

describe("AddAppModal — mode switching", () => {
  it("switches to Website mode when Website pill is clicked", () => {
    render(<AddAppModal {...makeProps()} />);
    fireEvent.click(screen.getByRole("radio", { name: "Website" }));
    expect(
      screen.getByRole("textbox", { name: "Website domain" }),
    ).toBeInTheDocument();
  });

  it("switching back to App mode hides the domain input", () => {
    render(<AddAppModal {...makeProps()} />);
    fireEvent.click(screen.getByRole("radio", { name: "Website" }));
    fireEvent.click(screen.getByRole("radio", { name: "App" }));
    expect(screen.queryByRole("textbox", { name: "Website domain" })).toBeNull();
  });

  it("shows the subdomain hint in Website mode", () => {
    render(<AddAppModal {...makeProps()} />);
    fireEvent.click(screen.getByRole("radio", { name: "Website" }));
    expect(
      screen.getByText(
        "Subdomains are separate sites (mail.google.com is not google.com).",
      ),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// App mode — multi-select + batch commit
// ---------------------------------------------------------------------------

describe("AddAppModal — App mode multi-select", () => {
  it("picking an app stages it and removes it from the list", () => {
    render(<AddAppModal {...makeProps()} />);
    // Chrome is in the list; click it.
    fireEvent.click(screen.getByText("Chrome"));
    // The list row for Chrome is now hidden (picked apps disappear). The pending
    // chip carries the same label, so disambiguate via the remove button.
    expect(
      screen.getByRole("button", { name: "Remove Chrome" }),
    ).toBeInTheDocument();
    // Slack remains pickable in the list.
    expect(screen.getByText("Slack")).toBeInTheDocument();
  });

  it("picking two apps then Add fires onAdd once with both rules + initial profile", () => {
    const onAdd = vi.fn();
    render(<AddAppModal {...makeProps({ onAdd })} />);
    fireEvent.click(screen.getByText("Chrome"));
    fireEvent.click(screen.getByText("Slack"));

    // Footer label reflects the count.
    const addBtn = screen.getByRole("button", { name: "Add 2 rules" });
    fireEvent.click(addBtn);

    expect(onAdd).toHaveBeenCalledOnce();
    expect(onAdd).toHaveBeenCalledWith(
      [
        { kind: "app", key: "chrome.exe" },
        { kind: "app", key: "slack.exe" },
      ],
      "chat",
    );
  });

  it("does NOT render the type-a-process-name row (Browse is the single fallback)", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(
      screen.queryByRole("textbox", { name: "Type a process name" }),
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Website mode — multi-add + batch commit
// ---------------------------------------------------------------------------

describe("AddAppModal — Website mode multi-add", () => {
  function switchToWebsite() {
    fireEvent.click(screen.getByRole("radio", { name: "Website" }));
  }

  it("the inline Add is disabled for a bare word without a dot", () => {
    render(<AddAppModal {...makeProps()} />);
    switchToWebsite();
    const input = screen.getByRole("textbox", { name: "Website domain" });
    fireEvent.change(input, { target: { value: "localhost" } });
    expect(screen.getByRole("button", { name: "Add website" })).toBeDisabled();
  });

  it("adding two domains stages both and Add fires onAdd with both site rules", () => {
    const onAdd = vi.fn();
    render(<AddAppModal {...makeProps({ onAdd })} />);
    switchToWebsite();
    const input = screen.getByRole("textbox", { name: "Website domain" });

    // Add github.com via Enter; input clears so a second can be typed.
    fireEvent.change(input, { target: { value: "github.com" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect((input as HTMLInputElement).value).toBe("");

    // Add a full URL via the inline Add button (scheme/path stripped).
    fireEvent.change(input, { target: { value: "https://example.com/x" } });
    fireEvent.click(screen.getByRole("button", { name: "Add website" }));

    expect(
      screen.getByRole("button", { name: "Remove github.com" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove example.com" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add 2 rules" }));
    expect(onAdd).toHaveBeenCalledOnce();
    expect(onAdd).toHaveBeenCalledWith(
      [
        { kind: "site", key: "github.com" },
        { kind: "site", key: "example.com" },
      ],
      "chat",
    );
  });

  it("a duplicate domain is not staged twice", () => {
    render(<AddAppModal {...makeProps()} />);
    switchToWebsite();
    const input = screen.getByRole("textbox", { name: "Website domain" });
    fireEvent.change(input, { target: { value: "github.com" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "github.com" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(
      screen.getAllByRole("button", { name: "Remove github.com" }),
    ).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Mixed apps + sites in one batch
// ---------------------------------------------------------------------------

describe("AddAppModal — mixed batch", () => {
  it("commits apps and sites staged across both modes in ONE onAdd call", () => {
    const onAdd = vi.fn();
    render(<AddAppModal {...makeProps({ onAdd })} />);
    // App mode: stage Chrome.
    fireEvent.click(screen.getByText("Chrome"));
    // Switch to Website mode and stage a domain.
    fireEvent.click(screen.getByRole("radio", { name: "Website" }));
    const input = screen.getByRole("textbox", { name: "Website domain" });
    fireEvent.change(input, { target: { value: "github.com" } });
    fireEvent.keyDown(input, { key: "Enter" });

    fireEvent.click(screen.getByRole("button", { name: "Add 2 rules" }));
    expect(onAdd).toHaveBeenCalledOnce();
    expect(onAdd).toHaveBeenCalledWith(
      [
        { kind: "app", key: "chrome.exe" },
        { kind: "site", key: "github.com" },
      ],
      "chat",
    );
  });
});

// ---------------------------------------------------------------------------
// Pending chip removal
// ---------------------------------------------------------------------------

describe("AddAppModal — pending chip removal", () => {
  it("removing a staged chip drops it from the batch", () => {
    const onAdd = vi.fn();
    render(<AddAppModal {...makeProps({ onAdd })} />);
    fireEvent.click(screen.getByText("Chrome"));
    fireEvent.click(screen.getByText("Slack"));
    // Remove Chrome.
    fireEvent.click(screen.getByRole("button", { name: "Remove Chrome" }));

    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(onAdd).toHaveBeenCalledWith(
      [{ kind: "app", key: "slack.exe" }],
      "chat",
    );
  });
});

// ---------------------------------------------------------------------------
// Commits to initialProfileId regardless of selection
// ---------------------------------------------------------------------------

describe("AddAppModal — commits to initialProfileId", () => {
  it("uses the initial profile id passed by the caller", () => {
    const onAdd = vi.fn();
    render(<AddAppModal {...makeProps({ onAdd, initialProfileId: "code" })} />);
    fireEvent.click(screen.getByText("Chrome"));
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(onAdd).toHaveBeenCalledWith(
      [{ kind: "app", key: "chrome.exe" }],
      "code",
    );
  });

  it("onClose is called after a successful commit", () => {
    const onClose = vi.fn();
    render(<AddAppModal {...makeProps({ onClose })} />);
    fireEvent.click(screen.getByText("Chrome"));
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});

// ---------------------------------------------------------------------------
// Footer + keyboard (Cancel / Esc discard)
// ---------------------------------------------------------------------------

describe("AddAppModal — footer and keyboard", () => {
  it("Cancel fires onClose and does not commit", () => {
    const onAdd = vi.fn();
    const onClose = vi.fn();
    render(<AddAppModal {...makeProps({ onAdd, onClose })} />);
    fireEvent.click(screen.getByText("Chrome"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(onAdd).not.toHaveBeenCalled();
  });

  it("Esc fires onClose", () => {
    const onClose = vi.fn();
    render(<AddAppModal {...makeProps({ onClose })} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});

// ---------------------------------------------------------------------------
// Reset on re-open
// ---------------------------------------------------------------------------

describe("AddAppModal — reset on re-open", () => {
  it("re-opening clears the pending list and resets to App mode", () => {
    const { rerender } = render(<AddAppModal {...makeProps()} />);
    // Stage an app and switch to Website mode.
    fireEvent.click(screen.getByText("Chrome"));
    fireEvent.click(screen.getByRole("radio", { name: "Website" }));

    rerender(<AddAppModal {...makeProps({ open: false })} />);
    rerender(<AddAppModal {...makeProps({ open: true })} />);

    // Back in App mode (domain input gone) with no pending chips.
    expect(screen.queryByRole("textbox", { name: "Website domain" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove Chrome" })).toBeNull();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Moving a rule off another profile (round-04 follow-up)
// ---------------------------------------------------------------------------

describe("AddAppModal — confirm move from another profile", () => {
  it("picking an app routed to another profile shows the confirm and does NOT stage it yet", () => {
    const onAdd = vi.fn();
    // Chrome is routed to "Chat"; the target here is "code".
    const otherProfileName = vi.fn((kind: "app" | "site", key: string) =>
      kind === "app" && key === "chrome.exe" ? "Chat" : null,
    );
    render(
      <AddAppModal
        {...makeProps({ onAdd, initialProfileId: "code", otherProfileName })}
      />,
    );

    fireEvent.click(screen.getByText("Chrome"));

    // Confirmation copy names the source + target profiles; scope to the
    // alertdialog because "Chrome" also still appears as a pickable list row.
    const confirm = screen.getByRole("alertdialog", {
      name: "Move rule to this profile",
    });
    expect(within(confirm).getByText("Chrome")).toBeInTheDocument();
    expect(within(confirm).getByText("Chat")).toBeInTheDocument();
    expect(within(confirm).getByText(/Move it to Code\?/)).toBeInTheDocument();
    // Not yet staged (no chip).
    expect(screen.queryByRole("button", { name: "Remove Chrome" })).toBeNull();
    // Footer Add stays disabled because nothing is staged.
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("clicking 'Move here' stages the app and dismisses the confirm", () => {
    const otherProfileName = vi.fn((kind: "app" | "site", key: string) =>
      kind === "app" && key === "chrome.exe" ? "Chat" : null,
    );
    render(
      <AddAppModal {...makeProps({ initialProfileId: "code", otherProfileName })} />,
    );

    fireEvent.click(screen.getByText("Chrome"));
    fireEvent.click(screen.getByRole("button", { name: "Move here" }));

    // Now staged (chip + its remove button), confirm gone.
    expect(
      screen.getByRole("button", { name: "Remove Chrome" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Move it to/)).toBeNull();
  });

  it("Cancel in the confirm leaves the pending list empty", () => {
    const otherProfileName = vi.fn((kind: "app" | "site", key: string) =>
      kind === "app" && key === "chrome.exe" ? "Chat" : null,
    );
    render(
      <AddAppModal {...makeProps({ initialProfileId: "code", otherProfileName })} />,
    );

    fireEvent.click(screen.getByText("Chrome"));
    // Two "Cancel" buttons now exist (the confirm card + the footer). The
    // confirm's Cancel is first in DOM order (it renders above the footer).
    const cancels = screen.getAllByRole("button", { name: "Cancel" });
    expect(cancels).toHaveLength(2);
    fireEvent.click(cancels[0]);

    // Confirm dismissed, nothing staged.
    expect(screen.queryByText(/Move it to/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove Chrome" })).toBeNull();
  });

  it("an app whose otherProfileName returns null stages directly (no confirm)", () => {
    const onAdd = vi.fn();
    // Slack is routed nowhere relevant -> null.
    const otherProfileName = vi.fn(() => null);
    render(
      <AddAppModal
        {...makeProps({ onAdd, initialProfileId: "code", otherProfileName })}
      />,
    );

    fireEvent.click(screen.getByText("Slack"));
    expect(screen.queryByText(/Move it to/)).toBeNull();
    expect(
      screen.getByRole("button", { name: "Remove Slack" }),
    ).toBeInTheDocument();
  });

  it("typing a website routed to another profile shows the confirm; Move here stages it", () => {
    const onAdd = vi.fn();
    const otherProfileName = vi.fn((kind: "app" | "site", key: string) =>
      kind === "site" && key === "github.com" ? "Chat" : null,
    );
    render(
      <AddAppModal
        {...makeProps({ onAdd, initialProfileId: "code", otherProfileName })}
      />,
    );

    fireEvent.click(screen.getByRole("radio", { name: "Website" }));
    const input = screen.getByRole("textbox", { name: "Website domain" });
    fireEvent.change(input, { target: { value: "github.com" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // Confirm shown, NOT staged, input retained.
    expect(screen.getByText("github.com")).toBeInTheDocument();
    expect(screen.getByText(/Move it to Code\?/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove github.com" })).toBeNull();
    expect((input as HTMLInputElement).value).toBe("github.com");

    // Move here stages the site and clears the input.
    fireEvent.click(screen.getByRole("button", { name: "Move here" }));
    expect(
      screen.getByRole("button", { name: "Remove github.com" }),
    ).toBeInTheDocument();
    expect((input as HTMLInputElement).value).toBe("");

    // Commit carries the moved site rule.
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(onAdd).toHaveBeenCalledWith(
      [{ kind: "site", key: "github.com" }],
      "code",
    );
  });
});

// ---------------------------------------------------------------------------
// Installed apps (merged after running) + Browse-for-an-app
// ---------------------------------------------------------------------------

describe("AddAppModal — installed apps + browse", () => {
  const INSTALLED_APPS: RunningApp[] = [
    { process_name: "chrome.exe", display_name: "Chrome", icon: null }, // dup of running
    { process_name: "notepad.exe", display_name: "Notepad", icon: null }, // not running
    { process_name: "code.exe", display_name: "Visual Studio Code", icon: null },
  ];

  it("lists installed apps that are not currently running", () => {
    render(<AddAppModal {...makeProps({ installedApps: INSTALLED_APPS })} />);
    // Running apps still present...
    expect(screen.getByText("Slack")).toBeInTheDocument();
    // ...and installed-but-not-running apps now appear too.
    expect(screen.getByText("Notepad")).toBeInTheDocument();
    expect(screen.getByText("Visual Studio Code")).toBeInTheDocument();
  });

  it("an app that is both running and installed appears only once", () => {
    render(<AddAppModal {...makeProps({ installedApps: INSTALLED_APPS })} />);
    expect(screen.getAllByText("Chrome")).toHaveLength(1);
  });

  it("ranks running apps before installed-only apps", () => {
    render(<AddAppModal {...makeProps({ installedApps: INSTALLED_APPS })} />);
    const names = screen
      .getAllByText(/^(Chrome|Slack|Notepad|Visual Studio Code)$/)
      .map((el) => el.textContent);
    // Running (Chrome, Slack) precede installed-only (Notepad, VS Code).
    expect(names.indexOf("Slack")).toBeLessThan(names.indexOf("Notepad"));
  });

  it("picking an installed-but-not-running app stages it as an app rule", () => {
    const onAdd = vi.fn();
    render(<AddAppModal {...makeProps({ onAdd, installedApps: INSTALLED_APPS })} />);
    fireEvent.click(screen.getByText("Notepad"));
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(onAdd).toHaveBeenCalledWith([{ kind: "app", key: "notepad.exe" }], "chat");
  });

  it("records the exe path on the rule when the picked app has a known path", () => {
    const onAdd = vi.fn();
    const withPath: RunningApp[] = [
      {
        process_name: "dota2.exe",
        display_name: "Dota 2",
        icon: null,
        path: "C:/Games/dota2.exe",
      },
    ];
    render(<AddAppModal {...makeProps({ onAdd, installedApps: withPath })} />);
    fireEvent.click(screen.getByText("Dota 2"));
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    // The exe path rides along so the host can record it into power_mode_paths.
    expect(onAdd).toHaveBeenCalledWith(
      [{ kind: "app", key: "dota2.exe", path: "C:/Games/dota2.exe" }],
      "chat",
    );
  });

  it("renders a Browse-for-an-app button in App mode", () => {
    render(<AddAppModal {...makeProps()} />);
    expect(
      screen.getByRole("button", { name: /Browse/ }),
    ).toBeInTheDocument();
  });

  it("hides the raw-path Browse flow when its capability presentation disables it", () => {
    render(<AddAppModal {...makeProps({ pathBrowseCapability: UNAVAILABLE })} />);
    expect(screen.queryByRole("button", { name: /Browse/ })).toBeNull();
  });

  it("disables unsupported rule types from their authoritative decisions", () => {
    render(
      <AddAppModal
        {...makeProps({
          appCapability: UNAVAILABLE,
          websiteCapability: UNAVAILABLE,
          pathBrowseCapability: UNAVAILABLE,
        })}
      />,
    );
    expect(screen.getByRole("radio", { name: "App" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Website" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Browse/ })).toBeNull();
  });

  it("defaults to Website when app identity is unsupported but browser identity is available", () => {
    render(
      <AddAppModal
        {...makeProps({
          appCapability: UNAVAILABLE,
          websiteCapability: AVAILABLE,
          pathBrowseCapability: UNAVAILABLE,
        })}
      />,
    );
    expect(screen.getByRole("radio", { name: "App" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Website" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("textbox", { name: "Website domain" })).toBeInTheDocument();
  });

  it("the Browse button is App-mode only (gone in Website mode)", () => {
    render(<AddAppModal {...makeProps()} />);
    fireEvent.click(screen.getByRole("radio", { name: "Website" }));
    expect(screen.queryByRole("button", { name: /Browse/ })).toBeNull();
  });
});


describe("Mac app identity presentation", () => {
  it("keeps opaque routing keys out of labels while preserving selection identity", () => {
    const identity = "macos-app:sha256:" + "a".repeat(64);
    const props = makeProps({
      runningApps: [{process_name: identity, display_name: "TextEdit", icon: null}],
    });
    render(<AddAppModal {...props} />);
    expect(screen.queryByText(/macos-app:/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "TextEdit" }));
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(props.onAdd).toHaveBeenCalledWith([{kind: "app", key: identity}], "chat");
  });
});
