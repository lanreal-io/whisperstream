/**
 * Vitest + Testing Library coverage for ProfileEditorModal (round 04 rebuild).
 *
 * Tests cover:
 *   - Renders nothing when open=false.
 *   - Header: "Profile Name" label + grounded input (issue 11).
 *   - Block label is "Activated when using" (issue 12b, locked copy).
 *   - Empty state shows "No apps or sites yet." (locked copy).
 *   - Plain "Add" button (issue 12b) fires onAddRule.
 *   - App targets render AppIcon + label + ✕; site targets render SiteGlyph +
 *     domain + ✕.
 *   - Removing an app/site chip fires onUnassign(kind, key).
 *   - Make/Remove default button: "Make default" on a non-default profile fires
 *     onSetDefault(true); "Remove as default" on the current default fires
 *     onSetDefault(false). The label tracks the default state.
 *   - Built-in profile prompt is read-only (no editable textarea, "Built-in"
 *     lock caption, no Template dropdown) (issue 9).
 *   - Custom profile prompt is an editable textarea with a Template dropdown.
 *   - Draft mode: footer shows Cancel + Create; Create fires onCommit; existing
 *     mode shows Done.
 *   - Inline icon field: the existing profile mark is the picker trigger,
 *     with a 5x3 roving-tabindex radiogroup, letter-fallback semantics,
 *     autosave through onIconChange, and a fixed built-in mark.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ProfileEditorModal } from "../ProfileEditorModal";
import type { Profile } from "../../lib/profiles";

// framer-motion must be mocked so AnimatePresence / motion.span don't need
// a browser animation engine in jsdom.
vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...p }: React.HTMLAttributes<HTMLDivElement>) => (
      <div {...p}>{children}</div>
    ),
    span: ({ children, ...p }: React.HTMLAttributes<HTMLSpanElement>) => (
      <span {...p}>{children}</span>
    ),
    svg: ({ children, ...p }: React.SVGAttributes<SVGSVGElement>) => (
      <svg {...p}>{children}</svg>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// Mock PromptTester — it makes API calls and uses motion; irrelevant to this
// file's concerns (chip rendering, label copy, remove callbacks).
vi.mock("../PromptTester", () => ({
  PromptTester: () => <div data-testid="prompt-tester" />,
}));

// Mock the bundled-prompt fetch so the fine-tune cleanup-mode picker doesn't hit
// the network in jsdom. Returns a deterministic body per mode; a custom
// profile's "Body" never matches, so the dropdown rests on its placeholder.
vi.mock("../../lib/api/enhancement", () => ({
    getBuiltinPrompt: vi.fn(async (mode: string) => ({
    mode,
    prompt: `BUILTIN_${mode}`,
  })),
  }));

// createPortal renders into document.body; in jsdom this works as-is.

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const CUSTOM_PROFILE: Profile = { id: "custom1", name: "Code", prompt: "Body" };
const BUILTIN_PROFILE: Profile = { id: "code", name: "Code", prompt: "Built-in body" };
const EMAIL_PROFILE: Profile = {
  id: "email",
  name: "Email",
  prompt: "Built-in email body",
  signature: "Best,\nAlex",
};

function makeProps(
  overrides: Partial<Parameters<typeof ProfileEditorModal>[0]> = {},
) {
  return {
    open: true,
    profile: CUSTOM_PROFILE,
    mode: "existing" as const,
    isDefault: false,
    isBuiltin: false,
    focusName: false,
    testerGate: "ready" as const,
    targets: [],
    hotkey: "",
    onHotkeyChange: vi.fn(),
    globalInputStyle: "clipboard",
    onInputStyleChange: vi.fn(),
    onIconChange: vi.fn(),
    onModeChange: vi.fn(),
    onSignatureChange: vi.fn(),
    send: vi.fn(() => true),
    subscribe: vi.fn(() => () => {}),
    connected: true,
    onSetDefault: vi.fn(),
    onPromptChange: vi.fn(),
    onRename: vi.fn(),
    onUnassign: vi.fn(),
    onAddRule: vi.fn(),
    addRuleDisabled: false,
    onDelete: vi.fn(),
    onCommit: vi.fn(),
    onClose: vi.fn(),
    reducedMotion: true,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Render guard
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — render guard", () => {
  it("renders nothing when open=false", () => {
    render(<ProfileEditorModal {...makeProps({ open: false })} />);
    expect(screen.queryByText("Activated when using")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Locked copy
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — locked copy", () => {
  it("shows the 'Profile Name' label associated with the name input", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    const input = screen.getByLabelText("Profile Name");
    expect(input).toBeInTheDocument();
    expect((input as HTMLInputElement).value).toBe("Code");
  });

  it("block label is 'Activated when using'", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    expect(screen.getByText("Activated when using")).toBeInTheDocument();
  });

  it("plain 'Add' button is present", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    expect(screen.getByRole("button", { name: /Add/i })).toBeInTheDocument();
  });

  it("empty state shows locked copy when targets is empty", () => {
    render(<ProfileEditorModal {...makeProps({ targets: [] })} />);
    expect(screen.getByText("No apps or sites yet.")).toBeInTheDocument();
  });

  it("clicking the Add button fires onAddRule", () => {
    const onAddRule = vi.fn();
    render(<ProfileEditorModal {...makeProps({ onAddRule })} />);
    fireEvent.click(screen.getByRole("button", { name: /^Add$/i }));
    expect(onAddRule).toHaveBeenCalledOnce();
  });
});

// ---------------------------------------------------------------------------
// App chip rendering
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — app chip rendering", () => {
  const appTarget = {
    kind: "app" as const,
    processName: "chrome.exe",
    label: "Chrome",
    icon: null,
  };

  it("renders an app chip with its label and a remove button", () => {
    render(<ProfileEditorModal {...makeProps({ targets: [appTarget] })} />);
    expect(screen.getByText("Chrome")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove Chrome from Code" }),
    ).toBeInTheDocument();
  });

  it("clicking app remove button fires onUnassign('app', processName)", () => {
    const onUnassign = vi.fn();
    render(
      <ProfileEditorModal {...makeProps({ targets: [appTarget], onUnassign })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove Chrome from Code" }));
    expect(onUnassign).toHaveBeenCalledWith("app", "chrome.exe");
  });
});

// ---------------------------------------------------------------------------
// Site chip rendering
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — site chip rendering", () => {
  const siteTarget = {
    kind: "site" as const,
    domain: "github.com",
    label: "github.com",
    icon: null,
  };

  it("renders domain text for a site chip", () => {
    render(<ProfileEditorModal {...makeProps({ targets: [siteTarget] })} />);
    expect(screen.getByText("github.com")).toBeInTheDocument();
  });

  it("clicking site remove button fires onUnassign('site', domain)", () => {
    const onUnassign = vi.fn();
    render(
      <ProfileEditorModal {...makeProps({ targets: [siteTarget], onUnassign })} />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Remove github.com from Code" }),
    );
    expect(onUnassign).toHaveBeenCalledWith("site", "github.com");
  });
});

// ---------------------------------------------------------------------------
// Make / Remove default button (round 04 follow-up — replaces the toggle)
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — make/remove default button", () => {
  it("a non-default profile shows 'Make default'; clicking it fires onSetDefault(true)", () => {
    const onSetDefault = vi.fn();
    render(<ProfileEditorModal {...makeProps({ isDefault: false, onSetDefault })} />);
    const btn = screen.getByRole("button", { name: "Make default" });
    expect(screen.queryByRole("button", { name: "Remove as default" })).toBeNull();
    fireEvent.click(btn);
    expect(onSetDefault).toHaveBeenCalledWith(true);
  });

  it("the current default shows 'Remove as default'; clicking it fires onSetDefault(false)", () => {
    const onSetDefault = vi.fn();
    render(<ProfileEditorModal {...makeProps({ isDefault: true, onSetDefault })} />);
    const btn = screen.getByRole("button", { name: "Remove as default" });
    expect(screen.queryByRole("button", { name: "Make default" })).toBeNull();
    fireEvent.click(btn);
    expect(onSetDefault).toHaveBeenCalledWith(false);
  });

  it("the default button label tracks the default state", () => {
    const { rerender } = render(
      <ProfileEditorModal {...makeProps({ isDefault: false })} />,
    );
    expect(screen.getByRole("button", { name: "Make default" })).toBeInTheDocument();
    rerender(<ProfileEditorModal {...makeProps({ isDefault: true })} />);
    expect(screen.getByRole("button", { name: "Remove as default" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Built-in vs custom prompt (issue 9, 12a)
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — prompt editability", () => {
  it("built-in profile prompt is hidden (no body, editable textarea, or Template)", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ profile: BUILTIN_PROFILE, isBuiltin: true })}
      />,
    );
    // Bundled prompt text is never rendered for Gemma/cloud either.
    expect(screen.queryByText("Built-in body")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Code cleanup prompt" })).toBeNull();
    expect(
      screen.getByText(/uses Whisperstream's built-in Code cleanup style/),
    ).toBeInTheDocument();
    // Built-in lock caption present; templates belong to custom profiles only.
    expect(screen.getByText("Built-in")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Template/ })).toBeNull();
  });

  it("custom profile prompt is an editable textarea with a Template dropdown", () => {
    render(<ProfileEditorModal {...makeProps({ isBuiltin: false })} />);
    const textarea = screen.getByLabelText("Code cleanup prompt");
    expect(textarea.tagName).toBe("TEXTAREA");
    // The shared Dropdown trigger carries aria-label "Template" with the current
    // selection appended ("Template: Template" while resting on the placeholder).
    expect(screen.getByRole("button", { name: /Template/ })).toBeInTheDocument();
  });

  it("editing the custom textarea fires onPromptChange", () => {
    const onPromptChange = vi.fn();
    render(<ProfileEditorModal {...makeProps({ onPromptChange })} />);
    fireEvent.change(screen.getByLabelText("Code cleanup prompt"), {
      target: { value: "New prompt body." },
    });
    expect(onPromptChange).toHaveBeenCalledWith("New prompt body.");
  });
});

// ---------------------------------------------------------------------------
// Fine-tune model: cleanup MODE picker instead of a free-text prompt (Feature 2)
// ---------------------------------------------------------------------------

describe("ProfileEditorModal - Email sign-off", () => {
  it("shows and edits the sign-off for the built-in Email profile", () => {
    const onSignatureChange = vi.fn();
    render(
      <ProfileEditorModal
        {...makeProps({
          profile: EMAIL_PROFILE,
          isBuiltin: true,
          onSignatureChange,
        })}
      />,
    );

    const signature = screen.getByLabelText("Signature");
    expect(signature).toHaveValue("Best,\nAlex");
    fireEvent.change(signature, { target: { value: "Regards,\nAlex" } });
    expect(onSignatureChange).toHaveBeenCalledWith("Regards,\nAlex");
  });

  it.each([
    {
      label: "Chat",
      profile: {
        id: "chat",
        name: "Chat",
        prompt: "Built-in chat body",
        signature: "Best,\nAlex",
      },
      isBuiltin: true,
    },
    {
      label: "custom",
      profile: {
        id: "custom1",
        name: "Custom",
        prompt: "Custom body",
        signature: "Best,\nAlex",
      },
      isBuiltin: false,
    },
  ])("hides a stale sign-off on $label profiles", ({ profile, isBuiltin }) => {
    render(<ProfileEditorModal {...makeProps({ profile, isBuiltin })} />);
    expect(screen.queryByLabelText("Signature")).toBeNull();
    expect(screen.queryByDisplayValue("Best,\nAlex")).toBeNull();
  });
});

describe("ProfileEditorModal — fixed-prompt model (Stream-1 Mini)", () => {
  const FIXED = {
    promptFixedByModel: true,
    trainedModesByModel: ["chat", "email"] as ("chat" | "email")[],
    fixedModelLabel: "Stream-1 Mini",
  };
  const GUIDANCE_CUSTOM =
    "Stream-1 Mini uses its own built-in prompts. To edit this one, switch to Customizable or a cloud model.";

  it("shows a custom profile's saved prompt as a dimmed one-line preview with guidance", () => {
    const onChangeModel = vi.fn();
    render(<ProfileEditorModal {...makeProps({ ...FIXED, onChangeModel })} />);
    // Same card title as editable models; no textarea, Template or info dot.
    expect(screen.getByText("Cleanup prompt")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Template/ })).toBeNull();
    expect(document.querySelector("textarea[aria-label='Code cleanup prompt']")).toBeNull();
    expect(screen.queryByRole("img", { name: /Why can't I edit/ })).toBeNull();

    const preview = screen.getByRole("textbox", { name: "Code cleanup prompt" });
    expect(preview.tagName).toBe("DIV");
    expect(preview).toHaveAttribute("aria-readonly", "true");
    expect(preview).toHaveAttribute("aria-disabled", "true");
    expect(preview).toHaveTextContent("Body");
    expect(preview).toHaveClass("truncate");
    expect(preview).toHaveAccessibleDescription(GUIDANCE_CUSTOM);

    fireEvent.click(screen.getByRole("button", { name: "Change model" }));
    expect(onChangeModel).toHaveBeenCalledOnce();
  });

  it("uses the empty-prompt placeholder for a custom profile with no saved prompt", () => {
    render(<ProfileEditorModal {...makeProps({ ...FIXED, profile: { id: "c2", name: "Notes", prompt: "  " } })} />);
    expect(screen.getByRole("textbox", { name: "Notes cleanup prompt" })).toHaveTextContent("No custom prompt yet");
  });

  it("keeps the Chat and Email mode choice and never rewrites the saved prompt", () => {
    const onModeChange = vi.fn();
    const onPromptChange = vi.fn();
    render(<ProfileEditorModal {...makeProps({
      ...FIXED,
      profile: { id: "custom1", name: "Code", prompt: "Body", mode: "email" },
      onModeChange,
      onPromptChange,
    })} />);
    fireEvent.click(screen.getByRole("button", { name: /Cleanup mode: Email/ }));
    expect(screen.queryByRole("option", { name: "Code" })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "Chat" }));
    expect(onModeChange).toHaveBeenCalledWith("chat");
    expect(onPromptChange).not.toHaveBeenCalled();
  });

  it("shows a built-in profile's style sentence with no prompt field and no switch action", () => {
    render(<ProfileEditorModal {...makeProps({
      ...FIXED,
      profile: { id: "email", name: "Email", prompt: "Built-in email body" },
      isBuiltin: true,
      onChangeModel: vi.fn(),
    })} />);
    expect(screen.queryByText("Built-in email body")).toBeNull();
    expect(screen.queryByRole("textbox", { name: /cleanup prompt/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Cleanup mode: Email/ })).toBeDisabled();
    expect(screen.getByText("Stream-1 Mini cleans this profile with its built-in Email style.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change model" })).toBeNull();
  });

  it("explains an unsupported mode, offers Change model and hides the tester", () => {
    const onChangeModel = vi.fn();
    render(<ProfileEditorModal {...makeProps({
      ...FIXED,
      profile: BUILTIN_PROFILE,
      isBuiltin: true,
      onChangeModel,
    })} />);
    expect(screen.getByText("Code isn't available with Stream-1 Mini")).toBeInTheDocument();
    expect(screen.getByText(
      "Dictation in this profile is inserted without cleanup. To clean it up, switch to Customizable or a cloud model.",
    )).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cleanup mode: Code/ })).toBeNull();
    expect(screen.queryByTestId("prompt-tester")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Change model" }));
    expect(onChangeModel).toHaveBeenCalledOnce();
  });

  it("omits Change model when the host gives no route (a draft profile)", () => {
    render(<ProfileEditorModal {...makeProps({ ...FIXED, mode: "draft" })} />);
    expect(screen.getByText(GUIDANCE_CUSTOM)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change model" })).toBeNull();
  });

  it("never fetches a shipped prompt under a fixed-prompt model", async () => {
    const api = await import("../../lib/api/enhancement");
    const getBuiltinPrompt = vi.mocked(api.getBuiltinPrompt);
    getBuiltinPrompt.mockClear();
    const { rerender } = render(<ProfileEditorModal {...makeProps({ ...FIXED })} />);
    rerender(<ProfileEditorModal {...makeProps({ ...FIXED, profile: BUILTIN_PROFILE, isBuiltin: true })} />);
    expect(getBuiltinPrompt).not.toHaveBeenCalled();
  });

  it("keeps the editable textarea for a customizable model (promptFixedByModel false)", () => {
    render(<ProfileEditorModal {...makeProps({ promptFixedByModel: false })} />);
    expect(screen.getByLabelText("Code cleanup prompt").tagName).toBe("TEXTAREA");
    expect(screen.queryByText(/uses its own built-in prompts/)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Hotkey card (issue 14)
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — hotkey card", () => {
  it("shows the Hotkey label and switch caption", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    expect(screen.getByText("Hotkey")).toBeInTheDocument();
    expect(screen.getByText("Press to switch to this profile.")).toBeInTheDocument();
  });

  it("renders the captured shortcut without a resting clear action", () => {
    render(<ProfileEditorModal {...makeProps({ hotkey: "ctrl+alt+1" })} />);
    expect(screen.getByText("Ctrl+Alt+1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear shortcut" })).toBeNull();
  });

  it("reveals Clear during capture and clicking it fires onHotkeyChange('')", () => {
    const onHotkeyChange = vi.fn();
    render(
      <ProfileEditorModal {...makeProps({ hotkey: "ctrl+alt+1", onHotkeyChange })} />,
    );
    fireEvent.click(screen.getByText("Ctrl+Alt+1"));
    fireEvent.click(screen.getByRole("button", { name: "Clear shortcut" }));
    expect(onHotkeyChange).toHaveBeenCalledWith("");
  });

  it("does not reveal Clear when capture begins without a hotkey", () => {
    render(<ProfileEditorModal {...makeProps({ hotkey: "" })} />);
    fireEvent.click(screen.getByText("Not set"));
    expect(screen.queryByRole("button", { name: "Clear shortcut" })).toBeNull();
  });

  it("the hotkey capture button is targeted at this profile", () => {
    const send = vi.fn(() => true);
    render(<ProfileEditorModal {...makeProps({ send })} />);
    // ShortcutCapture renders "Not set" when value is empty; clicking it starts
    // capture with the profile-scoped target.
    fireEvent.click(screen.getByText("Not set"));
    expect(send).toHaveBeenCalledWith({
      type: "shortcut.capture.start",
      target: "profile:custom1",
    });
  });
});

// ---------------------------------------------------------------------------
// Draft vs existing footer (issue 7)
// ---------------------------------------------------------------------------

describe("ProfileEditorModal — footer", () => {
  it("existing profile shows a Done button (no Create/Cancel)", () => {
    render(<ProfileEditorModal {...makeProps({ mode: "existing" })} />);
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create" })).toBeNull();
  });

  it("draft mode shows Cancel + Create (no Done, no Delete profile)", () => {
    const draft: Profile = { id: "draft1", name: "New profile", prompt: "" };
    render(
      <ProfileEditorModal {...makeProps({ mode: "draft", profile: draft })} />,
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Done" })).toBeNull();
    expect(screen.queryByText("Delete profile")).toBeNull();
  });

  it("draft Create fires onCommit(name, prompt)", () => {
    const onCommit = vi.fn();
    const draft: Profile = { id: "draft1", name: "Email", prompt: "Body" };
    render(
      <ProfileEditorModal
        {...makeProps({ mode: "draft", profile: draft, onCommit })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(onCommit).toHaveBeenCalledWith("Email", "Body");
  });

  it("draft Cancel fires onClose (discards)", () => {
    const onClose = vi.fn();
    const draft: Profile = { id: "draft1", name: "Email", prompt: "" };
    render(
      <ProfileEditorModal
        {...makeProps({ mode: "draft", profile: draft, onClose })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("a custom existing profile shows a quiet Delete profile button", () => {
    render(<ProfileEditorModal {...makeProps({ isBuiltin: false, mode: "existing" })} />);
    expect(screen.getByText("Delete profile")).toBeInTheDocument();
  });

  it("a built-in profile never shows Delete profile", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ profile: BUILTIN_PROFILE, isBuiltin: true })}
      />,
    );
    expect(screen.queryByText("Delete profile")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Inline icon field (transparent-indicators follow-up)
// ---------------------------------------------------------------------------

const GLYPH_LABELS = [
  "Chat",
  "Code",
  "Mail",
  "Document",
  "Note",
  "Terminal",
  "Checklist",
  "Calendar",
  "People",
  "Sparkle",
  "Search",
  "Pen",
  "Book",
  "Globe",
];

function iconCells() {
  if (screen.queryAllByRole("radio").length === 0) {
    fireEvent.click(screen.getByRole("button", { name: /Choose profile icon/ }));
  }
  return screen.getAllByRole("radio");
}

describe("ProfileEditorModal - inline icon field", () => {
  it("labels the existing profile mark as Icon beside Profile Name", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    expect(screen.getByText("Icon")).toBeInTheDocument();
    expect(screen.getByText("Profile Name")).toBeInTheDocument();
    expect(screen.queryByText("Shown in the Recording Indicator while you dictate.")).toBeNull();
    expect(screen.getByTestId("profile-mark-preview").closest("button")).toHaveAccessibleName(
      /Choose profile icon/,
    );
  });

  it("contains no em dash anywhere in the icon block copy", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    iconCells();
    const group = screen.getByRole("radiogroup", { name: "Profile icon" });
    const labels = iconCells().map((cell) => cell.getAttribute("aria-label") ?? "");
    expect(labels.join(" ")).not.toContain("—");
    expect(group.textContent ?? "").not.toContain("—");
  });

  it("renders a 15-cell radiogroup: the letter fallback then the 14 curated glyphs in order", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ profile: { id: "custom1", name: "Slack", prompt: "Body" } })}
      />,
    );
    iconCells();
    expect(screen.getByRole("radiogroup", { name: "Profile icon" })).toBeInTheDocument();
    const labels = iconCells().map((cell) => cell.getAttribute("aria-label"));
    expect(labels).toEqual(["First letter, S", ...GLYPH_LABELS]);
  });

  it("the letter cell announces the missing name as a state, not an error", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ profile: { id: "custom1", name: "   ", prompt: "Body" } })}
      />,
    );
    expect(iconCells()[0].getAttribute("aria-label")).toBe("First letter, no name yet");
  });

  it("the letter cell label tracks the live name draft", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ mode: "draft", profile: { id: "d1", name: "", prompt: "" } })}
      />,
    );
    expect(iconCells()[0].getAttribute("aria-label")).toBe("First letter, no name yet");
    fireEvent.change(screen.getByLabelText("Profile Name"), {
      target: { value: "notes" },
    });
    expect(iconCells()[0].getAttribute("aria-label")).toBe("First letter, N");
  });
});

describe("ProfileEditorModal - icon selection", () => {
  it("keeps the icon grid collapsed until the current icon is clicked", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    const trigger = screen.getByRole("button", { name: /Choose profile icon/ });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("radiogroup", { name: "Profile icon" })).toBeNull();
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });

  it("selects the letter cell when the profile has no stored icon", () => {
    render(<ProfileEditorModal {...makeProps()} />);
    const cells = iconCells();
    expect(cells[0].getAttribute("aria-checked")).toBe("true");
    expect(cells[0].getAttribute("tabindex")).toBe("0");
    expect(cells.slice(1).every((c) => c.getAttribute("tabindex") === "-1")).toBe(true);
  });

  it("selects the stored key and gives it the only tabbable cell", () => {
    render(
      <ProfileEditorModal
        {...makeProps({
          profile: { id: "custom1", name: "Research", prompt: "Body", icon: "search" },
        })}
      />,
    );
    iconCells();
    const search = screen.getByRole("radio", { name: "Search" });
    expect(search.getAttribute("aria-checked")).toBe("true");
    expect(search.getAttribute("tabindex")).toBe("0");
    expect(iconCells()[0].getAttribute("aria-checked")).toBe("false");
  });

  it("clicking a glyph cell autosaves through onIconChange", () => {
    const onIconChange = vi.fn();
    render(<ProfileEditorModal {...makeProps({ onIconChange })} />);
    iconCells();
    fireEvent.click(screen.getByRole("radio", { name: "Sparkle" }));
    expect(onIconChange).toHaveBeenCalledWith("sparkle");
    expect(screen.queryByRole("radiogroup", { name: "Profile icon" })).toBeNull();
  });

  it("clicking the letter cell clears the key (undefined), never a sentinel string", () => {
    const onIconChange = vi.fn();
    render(
      <ProfileEditorModal
        {...makeProps({
          onIconChange,
          profile: { id: "custom1", name: "Research", prompt: "Body", icon: "search" },
        })}
      />,
    );
    fireEvent.click(iconCells()[0]);
    expect(onIconChange).toHaveBeenCalledWith(undefined);
  });

  it("re-clicking the selected cell is a no-op, not a toggle-off", () => {
    const onIconChange = vi.fn();
    render(
      <ProfileEditorModal
        {...makeProps({
          onIconChange,
          profile: { id: "custom1", name: "Research", prompt: "Body", icon: "search" },
        })}
      />,
    );
    iconCells();
    fireEvent.click(screen.getByRole("radio", { name: "Search" }));
    expect(onIconChange).not.toHaveBeenCalled();
  });

  it("an unknown stored key degrades to the letter cell without crashing", () => {
    render(
      <ProfileEditorModal
        {...makeProps({
          profile: { id: "custom1", name: "Slack", prompt: "Body", icon: "rocket" },
        })}
      />,
    );
    expect(iconCells()[0].getAttribute("aria-checked")).toBe("true");
  });

  it("the header preview reflects the live selection", () => {
    render(
      <ProfileEditorModal
        {...makeProps({
          profile: { id: "custom1", name: "Research", prompt: "Body", icon: "search" },
        })}
      />,
    );
    const preview = screen.getByTestId("profile-mark-preview");
    expect(preview.getAttribute("data-profile-icon")).toBe("search");
  });

  it("the header preview follows the name draft for the letter fallback", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ mode: "draft", profile: { id: "d1", name: "", prompt: "" } })}
      />,
    );
    fireEvent.change(screen.getByLabelText("Profile Name"), {
      target: { value: "quotes" },
    });
    const preview = screen.getByTestId("profile-mark-preview");
    expect(preview.getAttribute("data-profile-icon")).toBe("letter");
    expect(preview.textContent).toBe("Q");
  });
});

describe("ProfileEditorModal - icon keyboard navigation", () => {
  function renderPicker() {
    const onIconChange = vi.fn();
    render(<ProfileEditorModal {...makeProps({ onIconChange })} />);
    iconCells();
    return {
      group: screen.getByRole("radiogroup", { name: "Profile icon" }),
      onIconChange,
    };
  }

  it("Arrow Right moves one cell forward and selects it", () => {
    const { group, onIconChange } = renderPicker();
    fireEvent.keyDown(group, { key: "ArrowRight" });
    expect(onIconChange).toHaveBeenCalledWith("chat");
    expect(document.activeElement).toBe(screen.getByRole("radio", { name: "Chat" }));
  });

  it("Arrow Left does not wrap past the first cell", () => {
    const { group, onIconChange } = renderPicker();
    fireEvent.keyDown(group, { key: "ArrowLeft" });
    expect(onIconChange).not.toHaveBeenCalled();
  });

  it("Arrow Down moves five cells (one row) and clamps at the last cell", () => {
    const { group, onIconChange } = renderPicker();
    // The grid is 5 wide, so a row step is +5 through the flat 15-cell order:
    // letter -> note -> sparkle -> (clamped) globe.
    fireEvent.keyDown(group, { key: "ArrowDown" });
    expect(onIconChange).toHaveBeenLastCalledWith("note");
    fireEvent.keyDown(group, { key: "ArrowDown" });
    expect(onIconChange).toHaveBeenLastCalledWith("sparkle");
    fireEvent.keyDown(group, { key: "ArrowDown" });
    expect(onIconChange).toHaveBeenLastCalledWith("globe");
  });

  it("End jumps to the last cell", () => {
    const { group, onIconChange } = renderPicker();
    fireEvent.keyDown(group, { key: "End" });
    expect(onIconChange).toHaveBeenLastCalledWith("globe");
    expect(document.activeElement).toBe(screen.getByRole("radio", { name: "Globe" }));
  });

  it("Home returns to the letter cell and clears the stored key", () => {
    const onIconChange = vi.fn();
    render(
      <ProfileEditorModal
        {...makeProps({
          onIconChange,
          profile: { id: "custom1", name: "Research", prompt: "Body", icon: "search" },
        })}
      />,
    );
    iconCells();
    fireEvent.keyDown(screen.getByRole("radiogroup", { name: "Profile icon" }), {
      key: "Home",
    });
    expect(onIconChange).toHaveBeenLastCalledWith(undefined);
  });

  it("Space and Enter select the focused cell", () => {
    const onIconChange = vi.fn();
    render(<ProfileEditorModal {...makeProps({ onIconChange })} />);
    iconCells();
    fireEvent.keyDown(screen.getByRole("radio", { name: "Mail" }), { key: " " });
    expect(onIconChange).toHaveBeenLastCalledWith("mail");
    iconCells();
    fireEvent.keyDown(screen.getByRole("radio", { name: "Pen" }), { key: "Enter" });
    expect(onIconChange).toHaveBeenLastCalledWith("pen");
  });
});

describe("ProfileEditorModal - built-in profiles keep a fixed icon", () => {
  it("renders a labeled fixed mark instead of the grid", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ profile: BUILTIN_PROFILE, isBuiltin: true })}
      />,
    );
    expect(screen.queryByRole("radiogroup", { name: "Profile icon" })).toBeNull();
    expect(screen.getByLabelText("Code uses its built-in icon")).toBeInTheDocument();
  });

  it("labels the fixed mark with the built-in name", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ profile: EMAIL_PROFILE, isBuiltin: true })}
      />,
    );
    expect(screen.getByLabelText("Email uses its built-in icon")).toBeInTheDocument();
  });

  it("shows the derived built-in stamp, never a selectable cell", () => {
    render(
      <ProfileEditorModal
        {...makeProps({ profile: BUILTIN_PROFILE, isBuiltin: true })}
      />,
    );
    expect(
      document.querySelectorAll('[data-profile-icon="code"]').length,
    ).toBeGreaterThan(0);
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
  });
});

describe("Profile dictation language", () => {
  it("inherits the default and offers only supported model languages", () => {
    const onLanguageChange = vi.fn();
    render(<ProfileEditorModal {...makeProps({ globalLanguage: "en", sttArtifact: "cohere-transcribe", onLanguageChange })} />);
    fireEvent.click(screen.getByRole("button", { name: /Dictation language for this profile:/ }));
    expect(screen.getByRole("option", { name: "Default (English)" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Russian" })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "French" }));
    expect(onLanguageChange).toHaveBeenCalledWith("fr");
    expect(screen.getByText("Applies even when enhancement is off. Uses the selected speech model.")).toBeInTheDocument();
  });

  it("preserves an unsupported saved choice and allows returning to default", () => {
    const onLanguageChange = vi.fn();
    render(<ProfileEditorModal {...makeProps({ profile: { ...CUSTOM_PROFILE, language: "fr" }, sttArtifact: "parakeet-v2", globalLanguage: "en", onLanguageChange })} />);
    expect(screen.getByText(/This language is not supported/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Dictation language for this profile:/ }));
    expect(screen.getByRole("option", { name: "French" })).toBeDisabled();
    fireEvent.click(screen.getByRole("option", { name: "Default (English)" }));
    expect(onLanguageChange).toHaveBeenCalledWith(undefined);
  });
});
