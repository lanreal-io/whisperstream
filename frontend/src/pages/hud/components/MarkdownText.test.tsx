import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  MarkdownText,
  markdownToPlainText,
  parseMarkdownInline,
} from "./MarkdownText";

afterEach(cleanup);

function renderMarkdown(text: string) {
  return render(<MarkdownText text={text} />).container.querySelector(
    '[data-testid="markdown-text"]',
  ) as HTMLElement;
}

describe("MarkdownText", () => {
  it("renders bold, italic, strikethrough and inline code as elements", () => {
    const root = renderMarkdown("A **bold** and *quiet* ~~old~~ `npm test` run.");
    expect(root.querySelector("strong")?.textContent).toBe("bold");
    expect(root.querySelector("em")?.textContent).toBe("quiet");
    expect(root.querySelector("del")?.textContent).toBe("old");
    expect(root.querySelector("code")?.textContent).toBe("npm test");
    expect(root.textContent).toBe("A bold and quiet old npm test run.");
  });

  it("keeps single line breaks inside a paragraph and splits on blank lines", () => {
    const root = renderMarkdown("First line\nsecond line\n\nNext paragraph");
    const paragraphs = root.querySelectorAll("p");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs[0].querySelectorAll("br")).toHaveLength(1);
    expect(paragraphs[0].textContent).toBe("First linesecond line");
  });

  it("renders headings at body size, lists and ordered lists", () => {
    render(
      <MarkdownText
        text={"## Result\n\n- one\n- **two**\n  - nested\n\n3. three\n4. four"}
      />,
    );
    expect(screen.getByRole("heading", { name: "Result" })).toBeInTheDocument();
    const bullets = document.querySelector("ul") as HTMLElement;
    expect(bullets.children).toHaveLength(2);
    expect(bullets.querySelector("li ul li")?.textContent).toBe("nested");
    const ordered = document.querySelector("ol") as HTMLOListElement;
    expect(ordered.getAttribute("start")).toBe("3");
    expect(ordered.children).toHaveLength(2);
  });

  it("renders fenced code verbatim, including an unterminated fence", () => {
    const root = renderMarkdown(
      "Run this:\n\n```ts\nconst a = **not bold**;\n  indented();\n```\n\n```\nopen",
    );
    const blocks = root.querySelectorAll("pre");
    expect(blocks).toHaveLength(2);
    expect(blocks[0].textContent).toBe("const a = **not bold**;\n  indented();");
    expect(blocks[0].querySelector("strong")).toBeNull();
    expect(blocks[1].textContent).toBe("open");
  });

  it("shows links as their label only and never creates anchors", () => {
    const root = renderMarkdown(
      "See [the docs](https://example.com/x) and ![diagram](a.png) or <https://example.com>.",
    );
    expect(root.querySelector("a")).toBeNull();
    expect(root.querySelector("img")).toBeNull();
    expect(root.textContent).toBe(
      "See the docs and diagram or https://example.com.",
    );
  });

  it("never interprets markup in agent text", () => {
    const root = renderMarkdown(
      '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)"> **<b>hi</b>**',
    );
    expect(document.querySelector("script")).toBeNull();
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("b")).toBeNull();
    expect(root.textContent).toContain("<script>alert(1)</script>");
    expect(root.querySelector("strong")?.textContent).toBe("<b>hi</b>");
  });

  it("leaves snake_case, lone stars and escaped markers alone", () => {
    const root = renderMarkdown("Use snake_case_name and 2 * 3 * 4 with \\*literal\\*.");
    expect(root.querySelector("em")).toBeNull();
    expect(root.textContent).toBe("Use snake_case_name and 2 * 3 * 4 with *literal*.");
  });

  it("pairs nested emphasis by run length", () => {
    const nodes = parseMarkdownInline("*a **b** c*");
    expect(nodes).toEqual([
      { kind: "em", children: ["a ", { kind: "strong", children: ["b"] }, " c"] },
    ]);
  });

  it("applies the reply field's size and the reading line height", () => {
    const root = renderMarkdown("Hello");
    expect(root.style.fontSize).toBe("13px");
    expect(root.style.lineHeight).toBe("1.55");
  });
});

describe("markdownToPlainText", () => {
  it("drops markers and link targets for accessible labels", () => {
    expect(
      markdownToPlainText(
        "**Hotkeys fixed.** `Enter` is claimed only while a [draft](https://x) is visible.\n\n- one\n- two",
      ),
    ).toBe("Hotkeys fixed. Enter is claimed only while a draft is visible. one. two");
  });
});
