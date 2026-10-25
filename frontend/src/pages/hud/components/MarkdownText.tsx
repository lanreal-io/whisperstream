import { type CSSProperties, type ReactNode, Fragment, memo } from "react";

// --- MarkdownText (agent-speak-fixes Task 2.3) ---
// Agent text arrives as Markdown. This renders the common subset as React
// elements: paragraphs, single line breaks, headings, bold, italic,
// strikethrough, inline code, fenced code, lists and quotes. There is no HTML
// path: every string reaches the DOM as a text child, so markup in agent text
// is shown, never interpreted. Links render as their label only; the HUD never
// opens URLs from agent text. HUD-private, used by the console timeline, the
// summary card and the compact pill's accessible label (via the plain-text
// helper below).

type ListItem = { depth: number; lines: string[] };

type Block =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "heading"; text: string }
  | { kind: "code"; text: string }
  | { kind: "list"; ordered: boolean; start: number; items: ListItem[] }
  | { kind: "quote"; lines: string[] }
  | { kind: "rule" };

const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const BULLET = /^( *)[-*+][ \t]+(.*)$/;
const ORDERED = /^( *)(\d{1,9})[.)][ \t]+(.*)$/;
const QUOTE = /^ {0,3}>[ \t]?(.*)$/;

/** Split agent text into blocks. Pure; exported for tests. */
export function parseMarkdownBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] | null = null;
  let list: Extract<Block, { kind: "list" }> | null = null;
  let quote: string[] | null = null;

  const closeOpen = () => {
    if (paragraph) blocks.push({ kind: "paragraph", lines: paragraph });
    if (list) blocks.push(list);
    if (quote) blocks.push({ kind: "quote", lines: quote });
    paragraph = null;
    list = null;
    quote = null;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    const fence = FENCE.exec(line);
    if (fence) {
      closeOpen();
      const marker = fence[1];
      const body: string[] = [];
      index += 1;
      while (index < lines.length) {
        const closing = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(lines[index]);
        if (
          closing &&
          closing[1][0] === marker[0] &&
          closing[1].length >= marker.length
        ) {
          break;
        }
        body.push(lines[index]);
        index += 1;
      }
      // An unterminated fence runs to the end, as it would while streaming.
      blocks.push({ kind: "code", text: body.join("\n") });
      continue;
    }

    if (line.trim() === "") {
      closeOpen();
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      closeOpen();
      blocks.push({ kind: "heading", text: heading[2] });
      continue;
    }

    if (RULE.test(line)) {
      closeOpen();
      blocks.push({ kind: "rule" });
      continue;
    }

    const bullet = BULLET.exec(line);
    const ordered = bullet ? null : ORDERED.exec(line);
    if (bullet || ordered) {
      const isOrdered = ordered !== null;
      const indent = (bullet ?? ordered)![1].length;
      const text = bullet ? bullet[2] : ordered![3];
      const depth = Math.min(3, Math.floor(indent / 2));
      const current = list as Extract<Block, { kind: "list" }> | null;
      if (current === null || (depth === 0 && current.ordered !== isOrdered)) {
        closeOpen();
        list = {
          kind: "list",
          ordered: isOrdered,
          start: ordered ? Number.parseInt(ordered[2], 10) : 1,
          items: [],
        };
      }
      (list as unknown as Extract<Block, { kind: "list" }>).items.push({
        depth,
        lines: [text],
      });
      continue;
    }

    const quoted = QUOTE.exec(line);
    if (quoted && paragraph === null && list === null) {
      if (quote === null) closeOpen();
      quote = quote ?? [];
      quote.push(quoted[1]);
      continue;
    }

    const openList = list as Extract<Block, { kind: "list" }> | null;
    if (openList !== null) {
      // Continuation of the current item (indented or lazy).
      openList.items[openList.items.length - 1].lines.push(line.trim());
      continue;
    }
    if (quote !== null) {
      (quote as string[]).push(line.trim());
      continue;
    }
    paragraph = paragraph ?? [];
    paragraph.push(line);
  }
  closeOpen();
  return blocks;
}

const ESCAPABLE = /[\\`*_{}[\]()#+\-.!~>|]/;

function isWordChar(char: string | undefined): boolean {
  return char !== undefined && /[\p{L}\p{N}]/u.test(char);
}

function isSpace(char: string | undefined): boolean {
  return char === undefined || /\s/.test(char);
}

/** A `[label](target)` at `start` (which must be "["). Returns the label and
 *  the index after the closing ")". The target is discarded. */
function matchLink(
  source: string,
  start: number,
): { label: string; end: number } | null {
  let depth = 0;
  let index = start;
  for (; index < source.length; index += 1) {
    const char = source[index];
    if (char === "\\") {
      index += 1;
      continue;
    }
    if (char === "\n" && depth > 0 && source[index + 1] === "\n") return null;
    if (char === "[") depth += 1;
    if (char === "]") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  if (depth !== 0 || source[index + 1] !== "(") return null;
  const close = source.indexOf(")", index + 2);
  if (close === -1) return null;
  const target = source.slice(index + 2, close);
  if (/\n/.test(target)) return null;
  return { label: source.slice(start + 1, index), end: close + 1 };
}

/** Find a closing delimiter run for `marker`, opened before `from`. Only a
 *  run of exactly the marker's length closes it, so `*a **b** c*` pairs the
 *  outer single stars and leaves the inner pair to the recursive parse. */
function findClosing(source: string, marker: string, from: number): number {
  const char = marker[0];
  let index = from;
  while (index < source.length) {
    const found = source.indexOf(char, index);
    if (found === -1) return -1;
    let length = 1;
    while (source[found + length] === char) length += 1;
    const before = source[found - 1];
    const after = source[found + length];
    if (
      found > from &&
      length === marker.length &&
      !isSpace(before) &&
      (char !== "_" || !isWordChar(after))
    ) {
      return found;
    }
    index = found + length;
  }
  return -1;
}

type InlineNode =
  | string
  | { kind: "code"; text: string }
  | { kind: "strong" | "em" | "del" | "link"; children: InlineNode[] };

/** Inline Markdown to a node tree. Pure; exported for tests. */
export function parseMarkdownInline(source: string): InlineNode[] {
  const out: InlineNode[] = [];
  let buffer = "";
  const flush = () => {
    if (buffer) out.push(buffer);
    buffer = "";
  };
  let index = 0;
  while (index < source.length) {
    const char = source[index];

    if (char === "\\" && ESCAPABLE.test(source[index + 1] ?? "")) {
      buffer += source[index + 1];
      index += 2;
      continue;
    }

    if (char === "`") {
      let run = 1;
      while (source[index + run] === "`") run += 1;
      let search = index + run;
      let close = -1;
      while (search < source.length) {
        const found = source.indexOf("`".repeat(run), search);
        if (found === -1) break;
        let length = run;
        while (source[found + length] === "`") length += 1;
        if (length === run) {
          close = found;
          break;
        }
        search = found + length;
      }
      if (close !== -1) {
        flush();
        let text = source.slice(index + run, close).replace(/\n/g, " ");
        if (text.length > 2 && text.startsWith(" ") && text.endsWith(" ") && text.trim()) {
          text = text.slice(1, -1);
        }
        out.push({ kind: "code", text });
        index = close + run;
        continue;
      }
      buffer += "`".repeat(run);
      index += run;
      continue;
    }

    if (char === "[" || (char === "!" && source[index + 1] === "[")) {
      const link = matchLink(source, char === "!" ? index + 1 : index);
      if (link) {
        flush();
        out.push({ kind: "link", children: parseMarkdownInline(link.label) });
        index = link.end;
        continue;
      }
    }

    if (char === "<") {
      const auto = /^<((?:https?|mailto):[^\s<>]+)>/i.exec(source.slice(index));
      if (auto) {
        flush();
        out.push(auto[1]);
        index += auto[0].length;
        continue;
      }
    }

    if (char === "*" || char === "_" || char === "~") {
      let run = 1;
      while (source[index + run] === char) run += 1;
      const before = source[index - 1];
      const after = source[index + run];
      const leftFlanking = !isSpace(after);
      const underscoreOpen = char !== "_" || !isWordChar(before);
      if (char === "~" && run === 2 && leftFlanking) {
        const close = findClosing(source, "~~", index + 2);
        if (close !== -1) {
          flush();
          out.push({ kind: "del", children: parseMarkdownInline(source.slice(index + 2, close)) });
          index = close + 2;
          continue;
        }
      }
      if (char !== "~" && leftFlanking && underscoreOpen && run <= 3) {
        const marker = char.repeat(run);
        const close = findClosing(source, marker, index + run);
        if (close !== -1) {
          flush();
          const inner = parseMarkdownInline(source.slice(index + run, close));
          out.push(
            run === 3
              ? { kind: "strong", children: [{ kind: "em", children: inner }] }
              : { kind: run === 2 ? "strong" : "em", children: inner },
          );
          index = close + run;
          continue;
        }
      }
      buffer += char.repeat(run);
      index += run;
      continue;
    }

    buffer += char;
    index += 1;
  }
  flush();
  return out;
}

function renderInline(nodes: InlineNode[], keyPrefix: string): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${keyPrefix}.${index}`;
    if (typeof node === "string") {
      const parts = node.split("\n");
      return (
        <Fragment key={key}>
          {parts.map((part, line) => (
            <Fragment key={line}>
              {line > 0 && <br />}
              {part}
            </Fragment>
          ))}
        </Fragment>
      );
    }
    if (node.kind === "code") return <code key={key}>{node.text}</code>;
    const children = renderInline(node.children, key);
    if (node.kind === "strong") return <strong key={key}>{children}</strong>;
    if (node.kind === "em") return <em key={key}>{children}</em>;
    if (node.kind === "del") return <del key={key}>{children}</del>;
    return <span key={key}>{children}</span>;
  });
}

/** Lines parsed as one inline run, so emphasis may span a line break, with
 *  each newline kept as `<br>`: agents use single newlines on purpose. */
function renderLines(lines: string[], keyPrefix: string): ReactNode[] {
  return renderInline(parseMarkdownInline(lines.join("\n")), keyPrefix);
}

function renderList(
  block: Extract<Block, { kind: "list" }>,
  keyPrefix: string,
): ReactNode {
  // Rebuild nesting from depths. Deeper items attach to the previous item.
  type Node = { item: ListItem; children: Node[] };
  const roots: Node[] = [];
  const stack: Node[] = [];
  for (const item of block.items) {
    const node: Node = { item, children: [] };
    while (stack.length > 0 && stack[stack.length - 1].item.depth >= item.depth) {
      stack.pop();
    }
    if (stack.length === 0) roots.push(node);
    else stack[stack.length - 1].children.push(node);
    stack.push(node);
  }
  const renderNodes = (nodes: Node[], prefix: string, ordered: boolean, start?: number): ReactNode => {
    const items = nodes.map((node, index) => (
      <li key={`${prefix}.${index}`}>
        {renderLines(node.item.lines, `${prefix}.${index}`)}
        {node.children.length > 0 && renderNodes(node.children, `${prefix}.${index}.sub`, ordered)}
      </li>
    ));
    return ordered ? (
      <ol key={prefix} start={start !== undefined && start !== 1 ? start : undefined}>{items}</ol>
    ) : (
      <ul key={prefix}>{items}</ul>
    );
  };
  return renderNodes(roots, keyPrefix, block.ordered, block.start);
}

export interface MarkdownTextProps {
  text: string;
  /** Message text matches the reply field (13px) by default. */
  fontSize?: number;
  lineHeight?: number;
  className?: string;
  style?: CSSProperties;
}

/** Agent Markdown as React elements. No `dangerouslySetInnerHTML`, ever. */
export const MarkdownText = memo(function MarkdownText({
  text,
  fontSize = 13,
  lineHeight = 1.55,
  className,
  style,
}: MarkdownTextProps) {
  const blocks = parseMarkdownBlocks(text);
  return (
    <div
      data-testid="markdown-text"
      className={className ? `hud-markdown ${className}` : "hud-markdown"}
      style={{ fontFamily: "var(--font-agent)", fontSize, lineHeight, ...style }}
    >
      {blocks.map((block, index) => {
        const key = `b${index}`;
        switch (block.kind) {
          case "paragraph":
            return <p key={key}>{renderLines(block.lines, key)}</p>;
          case "heading":
            // A heading at body size: weight carries the level, so a long
            // agent reply never jumps in scale inside a 13px reading column.
            return (
              <h4 key={key} className="hud-md-heading">
                {renderInline(parseMarkdownInline(block.text), key)}
              </h4>
            );
          case "code":
            return (
              <pre key={key}>
                <code>{block.text}</code>
              </pre>
            );
          case "list":
            return renderList(block, key);
          case "quote":
            return <blockquote key={key}>{renderLines(block.lines, key)}</blockquote>;
          case "rule":
            return <hr key={key} />;
        }
      })}
    </div>
  );
});

function plainInline(nodes: InlineNode[]): string {
  return nodes
    .map((node) =>
      typeof node === "string"
        ? node
        : node.kind === "code"
          ? node.text
          : plainInline(node.children),
    )
    .join("");
}

/** Markdown reduced to readable text for accessible labels and the compact
 *  pill: markers removed, link targets dropped, blocks joined by spaces. */
export function markdownToPlainText(text: string): string {
  return parseMarkdownBlocks(text)
    .map((block) => {
      switch (block.kind) {
        case "paragraph":
        case "quote":
          return plainInline(parseMarkdownInline(block.lines.join(" ")));
        case "heading":
          return plainInline(parseMarkdownInline(block.text));
        case "code":
          return block.text;
        case "list":
          return block.items
            .map((item) => plainInline(parseMarkdownInline(item.lines.join(" "))))
            .join(". ");
        case "rule":
          return "";
      }
    })
    .filter((part) => part.trim().length > 0)
    .join(" ")
    .replace(/[ \t]+/g, " ")
    .trim();
}
