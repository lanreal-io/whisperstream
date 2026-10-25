import { Children, isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { DocHeading } from "./articles";

const CALLOUT_LABELS = [
  "Note",
  "Security note",
  "Limitation",
  "Requires testing",
  "Important",
] as const;

function textFromChildren(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") {
        return String(child);
      }
      if (isValidElement<{ children?: ReactNode }>(child)) {
        return textFromChildren(child.props.children);
      }
      return "";
    })
    .join("");
}

function calloutType(children: ReactNode): string | undefined {
  const text = textFromChildren(children).trim();
  const label = CALLOUT_LABELS.find((candidate) =>
    text.toLowerCase().startsWith(`${candidate.toLowerCase()}:`),
  );
  return label?.toLowerCase().replaceAll(" ", "-");
}

export function DocsMarkdown({
  source,
  headings,
}: {
  source: string;
  headings: readonly DocHeading[];
}) {
  let headingIndex = 0;

  function heading(level: 2 | 3, children: ReactNode) {
    const fallback = level === 2 ? "section" : "detail";
    const current = headings[headingIndex++];
    const id = current?.id ?? fallback;
    const className = level === 2 ? "docs-prose-h2" : "docs-prose-h3";
    const content = (
      <a href={`#${id}`} className="docs-heading-link">
        <span>{children}</span>
        <span aria-hidden="true" className="docs-heading-hash">
          #
        </span>
      </a>
    );
    return level === 2 ? (
      <h2 id={id} className={className}>
        {content}
      </h2>
    ) : (
      <h3 id={id} className={className}>
        {content}
      </h3>
    );
  }

  const components: Components = {
    h1: ({ children }) => <h2 className="docs-prose-h2">{children}</h2>,
    h2: ({ children }) => heading(2, children),
    h3: ({ children }) => heading(3, children),
    h4: ({ children }) => <h4 className="docs-prose-h4">{children}</h4>,
    p: ({ children }) => <p>{children}</p>,
    a: ({ href, children }) => (
      <a href={href} className="docs-prose-link">
        {children}
      </a>
    ),
    blockquote: ({ children }) => {
      const type = calloutType(children);
      return (
        <blockquote
          className={type ? "docs-callout" : "docs-blockquote"}
          data-callout={type}
        >
          {children}
        </blockquote>
      );
    },
    code: ({ children, className }) => (
      <code className={`docs-code ${className ?? ""}`.trim()}>{children}</code>
    ),
    pre: ({ children }) => (
      <pre className="docs-code-block" tabIndex={0}>
        {children}
      </pre>
    ),
    table: ({ children }) => (
      <div
        role="region"
        aria-label="Table, scroll horizontally if needed"
        tabIndex={0}
        className="docs-table-scroll"
      >
        <table>{children}</table>
      </div>
    ),
    thead: ({ children }) => <thead>{children}</thead>,
    tbody: ({ children }) => <tbody>{children}</tbody>,
    tr: ({ children }) => <tr>{children}</tr>,
    th: ({ children }) => <th scope="col">{children}</th>,
    td: ({ children }) => <td>{children}</td>,
    hr: () => <hr />,
    img: ({ alt, ...props }) => (
      // Markdown authors control sources at build time. Keeping this as a
      // native image preserves support for diagrams without remote host config.
      // eslint-disable-next-line @next/next/no-img-element
      <img alt={alt ?? ""} {...props} />
    ),
  };

  return (
    <div className="docs-prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
