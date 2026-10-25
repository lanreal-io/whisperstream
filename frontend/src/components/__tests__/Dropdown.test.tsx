import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Dropdown, type DropdownOption } from "../Dropdown";

vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
      <div {...props}>{children}</div>
    ),
    svg: ({ children, ...props }: React.SVGAttributes<SVGSVGElement>) => (
      <svg {...props}>{children}</svg>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const options: DropdownOption<string>[] = [
  {
    value: "w2wrap1p2b-s3",
    label: "w2wrap1p2b-s3",
    description: "1.2B · Review candidate · Artifact available",
  },
  {
    value: "w8wrap_closure-s1",
    label: "w8wrap_closure-s1",
    description: "1.2B · Review candidate · Artifact unavailable",
    disabled: true,
  },
];

const groupedOptions: DropdownOption<string>[] = [
  {
    value: "ava",
    label: "Ava",
    description: "Voice 0",
    groupLabel: "Female voices",
  },
  {
    value: "brianna",
    label: "Brianna",
    description: "Voice 1",
    groupLabel: "Female voices",
    disabled: true,
  },
  {
    value: "frank",
    label: "Frank",
    description: "Voice 5",
    groupLabel: "Male voices",
  },
  {
    value: "george",
    label: "George",
    description: "Voice 6",
    groupLabel: "Male voices",
  },
];

function openDropdown(props: Partial<Parameters<typeof Dropdown<string>>[0]> = {}) {
  const onChange = vi.fn();
  render(
    <Dropdown
      value="w2wrap1p2b-s3"
      options={options}
      onChange={onChange}
      ariaLabel="Candidate model"
      {...props}
    />,
  );
  const trigger = screen.getByRole("button", { name: /Candidate model/ });
  fireEvent.click(trigger);
  return { onChange, trigger };
}

describe("Dropdown option descriptions", () => {
  it("shows the selected description in the trigger and keeps it in the accessible name", () => {
    openDropdown();

    expect(
      screen.getAllByText("1.2B · Review candidate · Artifact available"),
    ).toHaveLength(2);
    expect(screen.getByRole("button", { name: /Artifact available/ })).toHaveAttribute(
      "aria-label",
      expect.stringContaining("w2wrap1p2b-s3"),
    );
  });

  it("wraps long technical identifiers instead of ellipsizing them", () => {
    openDropdown();

    const optionLabel = screen
      .getByRole("option", { name: /w8wrap_closure-s1/ })
      .querySelector("[data-dropdown-option-label]");
    expect(optionLabel).toHaveStyle({ overflowWrap: "anywhere" });
    expect(optionLabel).not.toHaveClass("truncate");
  });
});

describe("Dropdown badges", () => {
  const badgeOptions: DropdownOption<string>[] = [
    {
      value: "flash-lite",
      label: "Gemini 3.5 Flash-Lite",
      badges: [
        { label: "Free Tier", tone: "mint" },
        { label: "Recommended", tone: "frost" },
      ],
    },
    { value: "flash", label: "Gemini 3.5 Flash", badges: [] },
  ];

  it("renders every chip in the badges array on the trigger and the option rows", () => {
    const onChange = vi.fn();
    render(
      <Dropdown
        value="flash-lite"
        options={badgeOptions}
        onChange={onChange}
        ariaLabel="Model"
      />,
    );
    // Trigger shows both chips for the current value.
    expect(screen.getByText("Free Tier")).toBeInTheDocument();
    expect(screen.getByText("Recommended")).toBeInTheDocument();
    // Open the panel: the recommended row still shows both chips.
    fireEvent.click(screen.getByRole("button", { name: /Model/ }));
    expect(screen.getAllByText("Free Tier").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Recommended").length).toBeGreaterThanOrEqual(1);
  });

  it("does not render a stray '0' for an option with an empty badges array", () => {
    // Regression: `opt.badges?.length` is 0 (falsy but a number), so a naive
    // `{cond && <chips/>}` guard renders the literal 0. The chip-less "Flash"
    // row must show no numeric artifact.
    const onChange = vi.fn();
    render(
      <Dropdown
        value="flash-lite"
        options={badgeOptions}
        onChange={onChange}
        ariaLabel="Model"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Model/ }));
    expect(screen.queryByText("0")).toBeNull();
  });
});

describe("Dropdown disabled options", () => {
  it("uses native and ARIA disabled semantics and cannot activate by mouse or keyboard", () => {
    const { onChange } = openDropdown();
    const disabledOption = screen.getByRole("option", {
      name: /w8wrap_closure-s1/,
    });

    expect(disabledOption).toBeDisabled();
    expect(disabledOption).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(disabledOption);
    fireEvent.keyDown(disabledOption, { key: "Enter" });
    disabledOption.focus();

    expect(onChange).not.toHaveBeenCalled();
    expect(disabledOption).not.toHaveFocus();
  });

  it("does not apply hover lift or render an action for a disabled option", () => {
    const renderOptionAction = vi.fn((option: DropdownOption<string>) => (
      <button data-testid={`action-${option.value}`}>Action</button>
    ));
    openDropdown({ renderOptionAction });
    const disabledOption = screen.getByRole("option", {
      name: /w8wrap_closure-s1/,
    });

    fireEvent.mouseEnter(disabledOption);

    expect(disabledOption.style.background).toBe("");
    expect(screen.queryByTestId("action-w8wrap_closure-s1")).toBeNull();
    expect(screen.getByTestId("action-w2wrap1p2b-s3")).toBeInTheDocument();
  });
});

describe("Dropdown semantic groups", () => {
  it("coalesces contiguous labelled options into visible accessible groups", () => {
    openDropdown({ options: groupedOptions, value: "ava" });

    const femaleGroup = screen.getByRole("group", { name: "Female voices" });
    const maleGroup = screen.getByRole("group", { name: "Male voices" });

    expect(femaleGroup).toContainElement(screen.getByRole("option", { name: /Ava/ }));
    expect(femaleGroup).toContainElement(screen.getByRole("option", { name: /Brianna/ }));
    expect(maleGroup).toContainElement(screen.getByRole("option", { name: /Frank/ }));
    expect(maleGroup).toContainElement(screen.getByRole("option", { name: /George/ }));
    expect(screen.getByText("Female voices")).toBeVisible();
    expect(screen.getByText("Male voices")).toBeVisible();
  });

  it("creates a new semantic group when the same label is not contiguous", () => {
    const splitGroups: DropdownOption<string>[] = [
      groupedOptions[0],
      groupedOptions[2],
      { ...groupedOptions[1], disabled: false },
    ];
    openDropdown({ options: splitGroups, value: "ava" });

    expect(screen.getAllByRole("group", { name: "Female voices" })).toHaveLength(2);
    expect(screen.getAllByText("Female voices")).toHaveLength(2);
  });

  it("preserves the flat DOM contract for callers without group metadata", () => {
    openDropdown();

    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(options.length);
  });
});

describe("Dropdown search", () => {
  it("filters long option lists across identifiers and descriptions", () => {
    openDropdown({ searchable: true, searchPlaceholder: "Search model runs" });
    const search = screen.getByRole("searchbox", { name: "Filter Candidate model" });

    expect(search).toHaveFocus();
    expect(search).toHaveAttribute("placeholder", "Search model runs");
    fireEvent.change(search, { target: { value: "closure" } });

    expect(screen.queryByRole("option", { name: /w2wrap1p2b-s3/ })).toBeNull();
    expect(screen.getByRole("option", { name: /w8wrap_closure-s1/ })).toBeInTheDocument();

    fireEvent.change(search, { target: { value: "available" } });
    expect(screen.getByRole("option", { name: /w2wrap1p2b-s3/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /w8wrap_closure-s1/ })).toBeInTheDocument();
  });

  it("shows an empty result and clears back to the complete list", () => {
    openDropdown({ searchable: true });
    const search = screen.getByRole("searchbox", { name: "Filter Candidate model" });
    fireEvent.change(search, { target: { value: "does-not-exist" } });

    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("No matching options")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getAllByRole("option")).toHaveLength(options.length);
    expect(search).toHaveFocus();
  });

  it("moves from the search field to the first enabled match with ArrowDown", () => {
    openDropdown({ searchable: true, options: groupedOptions, value: "ava" });
    const search = screen.getByRole("searchbox", { name: "Filter Candidate model" });
    fireEvent.change(search, { target: { value: "Frank" } });
    fireEvent.keyDown(search, { key: "ArrowDown" });

    expect(screen.getByRole("option", { name: /Frank/ })).toHaveFocus();
  });
});

describe("Dropdown keyboard interaction", () => {
  it("focuses the selected option when opened", () => {
    openDropdown({ options: groupedOptions, value: "george" });

    expect(screen.getByRole("option", { name: /George/ })).toHaveFocus();
  });

  it("falls back to the first enabled option when the selected option is disabled", () => {
    openDropdown({ options: groupedOptions, value: "brianna" });

    expect(screen.getByRole("option", { name: /Ava/ })).toHaveFocus();
  });

  it("moves across groups, wraps, and skips disabled options with arrow keys", () => {
    openDropdown({ options: groupedOptions, value: "ava" });
    const ava = screen.getByRole("option", { name: /Ava/ });

    fireEvent.keyDown(ava, { key: "ArrowDown" });
    expect(screen.getByRole("option", { name: /Frank/ })).toHaveFocus();

    fireEvent.keyDown(screen.getByRole("option", { name: /Frank/ }), {
      key: "ArrowUp",
    });
    expect(ava).toHaveFocus();

    fireEvent.keyDown(ava, { key: "ArrowUp" });
    expect(screen.getByRole("option", { name: /George/ })).toHaveFocus();
  });

  it("moves to the first and last enabled option with Home and End", () => {
    openDropdown({ options: groupedOptions, value: "frank" });
    const frank = screen.getByRole("option", { name: /Frank/ });

    fireEvent.keyDown(frank, { key: "Home" });
    expect(screen.getByRole("option", { name: /Ava/ })).toHaveFocus();

    fireEvent.keyDown(screen.getByRole("option", { name: /Ava/ }), { key: "End" });
    expect(screen.getByRole("option", { name: /George/ })).toHaveFocus();
  });

  it("wraps typeahead to the next enabled label match", () => {
    const matchingOptions: DropdownOption<string>[] = [
      { value: "ava", label: "Ava" },
      { value: "brianna", label: "Brianna", disabled: true },
      { value: "bella", label: "Bella" },
      { value: "blake", label: "Blake" },
    ];
    openDropdown({ options: matchingOptions, value: "blake" });
    const blake = screen.getByRole("option", { name: "Blake" });

    fireEvent.keyDown(blake, { key: "b" });
    expect(screen.getByRole("option", { name: "Bella" })).toHaveFocus();
  });

  it("selects the focused option with Enter or Space", () => {
    const { onChange } = openDropdown({ options: groupedOptions, value: "ava" });
    const ava = screen.getByRole("option", { name: /Ava/ });

    fireEvent.keyDown(ava, { key: "ArrowDown" });
    const frank = screen.getByRole("option", { name: /Frank/ });
    fireEvent.keyDown(frank, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("frank");
    expect(screen.queryByRole("listbox")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Candidate model/ }));
    const reopenedAva = screen.getByRole("option", { name: /Ava/ });
    fireEvent.keyDown(reopenedAva, { key: "End" });
    fireEvent.keyDown(screen.getByRole("option", { name: /George/ }), { key: " " });

    expect(onChange).toHaveBeenLastCalledWith("george");
  });

  it("closes on Escape and restores focus to the trigger", () => {
    const { trigger } = openDropdown({ options: groupedOptions, value: "ava" });

    fireEvent.keyDown(screen.getByRole("option", { name: /Ava/ }), {
      key: "Escape",
    });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("closes on Tab without trapping focus on the trigger", () => {
    const { trigger } = openDropdown({ options: groupedOptions, value: "ava" });

    fireEvent.keyDown(screen.getByRole("option", { name: /Ava/ }), { key: "Tab" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).not.toHaveFocus();
  });
});

describe("Dropdown focusable row actions", () => {
  it("keeps the panel open on Tab so the row action can be reached", () => {
    openDropdown({
      options: groupedOptions,
      value: "ava",
      optionActionsFocusable: true,
      renderOptionAction: (opt) => (
        <button type="button" aria-label={`Delete ${opt.label}`} />
      ),
    });

    fireEvent.keyDown(screen.getByRole("option", { name: /Ava/ }), { key: "Tab" });

    expect(screen.queryByRole("listbox")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Delete Ava" })).toBeInTheDocument();
  });

  it("closes once focus leaves the panel entirely", () => {
    openDropdown({
      options: groupedOptions,
      value: "ava",
      optionActionsFocusable: true,
      renderOptionAction: (opt) => (
        <button type="button" aria-label={`Delete ${opt.label}`} />
      ),
    });

    fireEvent.blur(screen.getByRole("listbox"), { relatedTarget: document.body });

    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("stays open when focus moves between elements inside the panel", () => {
    openDropdown({
      options: groupedOptions,
      value: "ava",
      optionActionsFocusable: true,
      renderOptionAction: (opt) => (
        <button type="button" aria-label={`Delete ${opt.label}`} />
      ),
    });

    fireEvent.blur(screen.getByRole("listbox"), {
      relatedTarget: screen.getByRole("button", { name: "Delete Ava" }),
    });

    expect(screen.queryByRole("listbox")).not.toBeNull();
  });

  it("closes, not reopens, when the trigger is clicked again", () => {
    const { trigger } = openDropdown({
      options: groupedOptions,
      value: "ava",
      optionActionsFocusable: true,
      renderOptionAction: (opt) => (
        <button type="button" aria-label={`Delete ${opt.label}`} />
      ),
    });

    // WebKit does not focus a clicked button, so the panel blurs on mousedown
    // with no related target before the trigger's click toggles.
    fireEvent.mouseDown(trigger);
    fireEvent.blur(screen.getByRole("listbox"), { relatedTarget: null });
    fireEvent.mouseUp(trigger);
    fireEvent.click(trigger);

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });
});

describe("Dropdown viewport clamping", () => {
  it("keeps the default panel internally scrollable within the available viewport", () => {
    const previousHeight = window.innerHeight;
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 160 });

    const onChange = vi.fn();
    render(
      <Dropdown
        value="w2wrap1p2b-s3"
        options={options}
        onChange={onChange}
        ariaLabel="Candidate model"
      />,
    );
    const trigger = screen.getByRole("button", { name: /Candidate model/ });
    vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue({
      x: 20,
      y: 60,
      left: 20,
      right: 220,
      top: 60,
      bottom: 90,
      width: 200,
      height: 30,
      toJSON: () => ({}),
    });
    fireEvent.click(trigger);

    const panel = screen.getByRole("listbox");
    expect(panel).toHaveClass("overflow-y-auto");
    expect(panel).toHaveStyle({ maxHeight: "54px" });

    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: previousHeight,
    });
  });
});

describe("Dropdown scroll dismissal", () => {
  it("stays open when its portal panel scrolls", () => {
    openDropdown();

    fireEvent.scroll(screen.getByRole("listbox"));

    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("still closes when the window scrolls", () => {
    openDropdown();

    fireEvent.scroll(window);

    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("still closes when an ancestor scrolls", () => {
    const { trigger } = openDropdown();

    fireEvent.scroll(trigger.parentElement as HTMLElement);

    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("still closes when the window resizes", () => {
    openDropdown();

    fireEvent.resize(window);

    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("Dropdown opt-in model-picker props", () => {
  const modelOptions: DropdownOption<string>[] = [
    {
      value: "mini",
      label: "Stream-1 Mini",
      description: "Fast cleanup for Chat and Email",
      descriptionInOptionsOnly: true,
      badge: { label: "Recommended", tone: "frost" },
      badgeInOptionsOnly: true,
      badgeInline: true,
      adornment: <span data-testid="row-meta">462 MB</span>,
      triggerAdornment: <span data-testid="trigger-meta">38%</span>,
    },
    {
      value: "custom",
      label: "Customizable",
      description: "Write your own prompts. Built on Gemma 4.",
      descriptionContent: <span data-testid="rich-description">Rich line</span>,
      descriptionInOptionsOnly: true,
      disabled: true,
      actionWhenDisabled: true,
    },
  ];

  it("keeps panel-only descriptions and badges off the trigger and its accessible name", () => {
    render(
      <Dropdown value="mini" options={modelOptions} onChange={vi.fn()} ariaLabel="Local model" />,
    );
    const trigger = screen.getByRole("button", { name: "Local model: Stream-1 Mini" });
    expect(trigger).not.toHaveTextContent("Fast cleanup");
    expect(trigger).not.toHaveTextContent("Recommended");
    // The trigger-only adornment replaces the row adornment on the trigger.
    expect(screen.getByTestId("trigger-meta")).toBeInTheDocument();
    expect(screen.queryByTestId("row-meta")).toBeNull();

    fireEvent.click(trigger);
    const row = screen.getByRole("option", { name: /Stream-1 Mini/ });
    expect(row).toHaveTextContent("Fast cleanup for Chat and Email");
    expect(within(row).getByTestId("row-meta")).toBeInTheDocument();
    // Inline badge sits on the label line, not in a trailing chip group.
    const label = row.querySelector("[data-dropdown-option-label]")!;
    expect(label).toHaveTextContent("Recommended");
  });

  it("renders rich panel descriptions and actions on disabled rows only when asked", () => {
    render(
      <Dropdown
        value="mini"
        options={modelOptions}
        onChange={vi.fn()}
        ariaLabel="Local model"
        renderOptionAction={(opt) => (
          <button type="button" aria-label={`Delete ${opt.label}`} />
        )}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Local model/ }));
    expect(screen.getByTestId("rich-description")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Customizable/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete Customizable" })).toBeInTheDocument();
  });

  it("still drops actions on disabled rows by default", () => {
    render(
      <Dropdown
        value="w2wrap1p2b-s3"
        options={options}
        onChange={vi.fn()}
        ariaLabel="Candidate model"
        renderOptionAction={(opt) => (
          <button type="button" aria-label={`Delete ${opt.label}`} />
        )}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Candidate model/ }));
    expect(screen.getByRole("button", { name: "Delete w2wrap1p2b-s3" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete w8wrap_closure-s1" })).toBeNull();
  });

  it("opens and focuses on each new openRequest, never on zero", () => {
    const { rerender } = render(
      <Dropdown value="mini" options={modelOptions} onChange={vi.fn()} ariaLabel="Local model" openRequest={0} />,
    );
    expect(screen.queryByRole("listbox")).toBeNull();
    rerender(
      <Dropdown value="mini" options={modelOptions} onChange={vi.fn()} ariaLabel="Local model" openRequest={1} />,
    );
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Stream-1 Mini/ })).toHaveFocus();
  });
});
