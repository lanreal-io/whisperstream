export const DOC_STATUSES = ["placeholder", "published"] as const;

export type DocStatus = (typeof DOC_STATUSES)[number];

export const DOC_GROUP_DEFINITIONS = [
  { id: "getting-started", title: "Getting started" },
  { id: "dictation", title: "Dictation" },
  { id: "enhancement", title: "AI enhancement" },
  { id: "data-privacy", title: "Data and privacy" },
  { id: "workflows-integrations", title: "Workflows and integrations" },
  { id: "reference-support", title: "Reference and support" },
] as const;

export type DocGroupId = (typeof DOC_GROUP_DEFINITIONS)[number]["id"];

export type DocArticle = {
  readonly id: string;
  readonly group: DocGroupId;
  readonly path: string;
  readonly navTitle: string;
  readonly title: string;
  readonly description: string;
  readonly source: string;
  readonly status: DocStatus;
  readonly appliesTo: string;
  readonly lastReviewed: string;
};

const SHARED_METADATA: Pick<
  DocArticle,
  "appliesTo" | "lastReviewed" | "status"
> = {
  appliesTo: "Whisperstream 1.5.0",
  lastReviewed: "2026-08-23",
  status: "placeholder",
};

export const DOC_ARTICLES = [
  {
    id: "install-get-started",
    group: "getting-started",
    path: "/docs",
    navTitle: "Install and configure",
    title: "Install and configure Whisperstream",
    description:
      "Install Whisperstream on Windows or Mac, complete first-run setup, and make your first dictation.",
    source: "install-get-started.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "app-basics",
    group: "getting-started",
    path: "/docs/getting-started/app-basics",
    navTitle: "App basics and updates",
    title: "App basics, licensing, updates, and uninstall",
    description:
      "Learn how the tray app, startup option, trial and license, updates, support links, and uninstall work.",
    source: "app-basics.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "recording-controls",
    group: "dictation",
    path: "/docs/dictation/recording-controls",
    navTitle: "Recording controls",
    title: "Recording controls and last-transcript actions",
    description:
      "Configure Push to Talk, Toggle, the recording indicator, and shortcuts that reuse the most recent dictation.",
    source: "recording-controls.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "languages-models",
    group: "dictation",
    path: "/docs/dictation/languages-models",
    navTitle: "Languages and models",
    title: "Languages and speech recognition models",
    description:
      "Choose a dictation language, compare supported speech models, and follow multilingual recording guidance.",
    source: "languages-models.md",
    ...SHARED_METADATA,
    appliesTo: "Whisperstream 1.5.1",
    lastReviewed: "2026-09-07",
    status: "published",
  },
  {
    id: "audio",
    group: "dictation",
    path: "/docs/dictation/audio",
    navTitle: "Audio",
    title: "Microphone, playback, and system audio",
    description:
      "Choose a microphone, configure recording chimes, and control whether Whisperstream leaves, lowers, or mutes other audio while recording.",
    source: "audio.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "live-preview",
    group: "dictation",
    path: "/docs/dictation/live-preview",
    navTitle: "Live Preview",
    title: "Live Preview",
    description:
      "Understand provisional text in the recording display and how it differs from the final transcript.",
    source: "live-preview.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "text-insertion",
    group: "dictation",
    path: "/docs/dictation/text-insertion",
    navTitle: "Text insertion",
    title: "Text insertion and compatibility",
    description:
      "Choose clipboard or keystroke insertion and test applications with accessibility or remote-session constraints.",
    source: "text-insertion.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "dictionary",
    group: "dictation",
    path: "/docs/dictation/dictionary",
    navTitle: "Dictionary",
    title: "Dictionary filters and replacements",
    description:
      "Filter unwanted words and replace recurring recognized terms in completed dictation.",
    source: "dictionary.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "enhancement-providers",
    group: "enhancement",
    path: "/docs/enhancement/providers",
    navTitle: "Enhancement providers",
    title: "Local, Ollama, and cloud enhancement providers",
    description:
      "Choose and configure where optional transcript cleanup runs.",
    source: "enhancement-providers.md",
    ...SHARED_METADATA,
    appliesTo: "Whisperstream 1.5.1",
    lastReviewed: "2026-09-07",
    status: "published",
  },
  {
    id: "enhancement-profiles-routing",
    group: "enhancement",
    path: "/docs/enhancement/profiles-routing",
    navTitle: "Profiles and routing",
    title: "Profiles, Email mode, app detection, and site detection",
    description:
      "Configure built-in and custom profiles, then route them by manual choice, website, or foreground application.",
    source: "enhancement-profiles-routing.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "transcript-history",
    group: "data-privacy",
    path: "/docs/data/transcript-history",
    navTitle: "Transcript history",
    title: "Transcript and audio history",
    description:
      "Control saved transcript and audio history, recovery keys, retention, search, playback, copying, import, and deletion.",
    source: "transcript-history.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "lockdown-mode",
    group: "data-privacy",
    path: "/docs/data/lockdown-mode",
    navTitle: "Lockdown Mode",
    title: "Lockdown Mode",
    description:
      "Enable, use, recover, or turn off password-gated Lockdown Mode.",
    source: "lockdown-mode.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "managed-lockdown-deployment",
    group: "data-privacy",
    path: "/docs/data/managed-lockdown-deployment",
    navTitle: "Managed Lockdown",
    title: "Managed Lockdown for organizations",
    description:
      "Understand Managed Lockdown capabilities, user controls, security boundaries, and how to request enterprise deployment guidance.",
    source: "managed-lockdown-deployment.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "agent-speak",
    group: "workflows-integrations",
    path: "/docs/workflows/agent-speak",
    navTitle: "Agent Speak",
    title: "Agent Speak for Codex and Claude Code",
    description:
      "Connect Codex or Claude Code, read or hear short summaries, and reply through the HUD.",
    source: "agent-speak.md",
    ...SHARED_METADATA,
    appliesTo: "Whisperstream 1.5.1",
    lastReviewed: "2026-09-07",
    status: "published",
  },
  {
    id: "coding-agent-sessions",
    group: "workflows-integrations",
    path: "/docs/workflows/coding-agent-sessions",
    navTitle: "Coding agent sessions",
    title: "Managed Codex and Claude Code sessions",
    description:
      "Start Codex or Claude Code through the Whisperstream CLI so sessions remain available for HUD and voice replies.",
    source: "coding-agent-sessions.md",
    ...SHARED_METADATA,
    status: "published",
  },
  {
    id: "settings-data-locations",
    group: "reference-support",
    path: "/docs/reference/settings-data-locations",
    navTitle: "Settings and data locations",
    title: "Settings and data locations",
    description:
      "Find settings and local data, move downloaded models, export logs, and choose what survives uninstall.",
    source: "settings-data-locations.md",
    ...SHARED_METADATA,
    status: "published",
  },
] as const satisfies readonly DocArticle[];

export type RegisteredDocArticle = (typeof DOC_ARTICLES)[number];

export type DocGroup = {
  readonly id: DocGroupId;
  readonly title: string;
  readonly articles: readonly RegisteredDocArticle[];
};

export const DOC_GROUPS: readonly DocGroup[] = DOC_GROUP_DEFINITIONS.map(
  (group) => ({
    ...group,
    articles: DOC_ARTICLES.filter((article) => article.group === group.id),
  }),
);

export type DocHeading = {
  readonly depth: 2 | 3;
  readonly id: string;
  readonly text: string;
};

const articleByPath = new Map<string, RegisteredDocArticle>(
  DOC_ARTICLES.map((article) => [article.path, article]),
);

function normalizeDocPath(path: string): string {
  const withoutQuery = path.split(/[?#]/, 1)[0] || "/docs";
  if (withoutQuery === "/") return "/docs";
  return withoutQuery.length > 1
    ? withoutQuery.replace(/\/+$/, "")
    : withoutQuery;
}

export function getDocByPath(path: "/docs"): RegisteredDocArticle;
export function getDocByPath(path: string): RegisteredDocArticle | undefined;
export function getDocByPath(path: string): RegisteredDocArticle | undefined {
  return articleByPath.get(normalizeDocPath(path));
}

export function getDocBySlug(
  slug: readonly string[] | undefined,
): RegisteredDocArticle | undefined {
  const path = slug?.length ? `/docs/${slug.join("/")}` : "/docs";
  return getDocByPath(path);
}

export function getPublishedDocs(): readonly RegisteredDocArticle[] {
  return DOC_ARTICLES.filter((article) => article.status === "published");
}

export function isDocPublished(article: DocArticle): boolean {
  return article.status === "published";
}

export function getDocNeighbors(article: DocArticle): {
  readonly previous: RegisteredDocArticle | undefined;
  readonly next: RegisteredDocArticle | undefined;
} {
  const index = DOC_ARTICLES.findIndex((candidate) => candidate.path === article.path);
  if (index === -1) return { previous: undefined, next: undefined };
  return {
    previous: DOC_ARTICLES[index - 1],
    next: DOC_ARTICLES[index + 1],
  };
}

function headingText(markdown: string): string {
  return markdown
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/<[^>]+>/g, "")
    .replace(/[`*_~]/g, "")
    .trim();
}

export function slugifyDocHeading(value: string): string {
  return headingText(value)
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function extractDocHeadings(markdown: string): readonly DocHeading[] {
  const headings: DocHeading[] = [];
  const occurrences = new Map<string, number>();
  const matcher = /^(#{2,3})[ \t]+(.+?)[ \t]*#*[ \t]*$/gm;

  for (const match of markdown.matchAll(matcher)) {
    const text = headingText(match[2]);
    const baseId = slugifyDocHeading(text) || "section";
    const count = (occurrences.get(baseId) ?? 0) + 1;
    occurrences.set(baseId, count);
    headings.push({
      depth: match[1].length as 2 | 3,
      id: count === 1 ? baseId : `${baseId}-${count}`,
      text,
    });
  }

  return headings;
}

function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}

export function validateDocsRegistry(
  articles: readonly DocArticle[],
): readonly string[] {
  const errors: string[] = [];
  const seenIds = new Set<string>();
  const seenPaths = new Set<string>();
  const seenSources = new Set<string>();
  const groupIds = new Set<string>(DOC_GROUP_DEFINITIONS.map((group) => group.id));
  const statuses = new Set<string>(DOC_STATUSES);

  for (const article of articles) {
    if (seenIds.has(article.id)) errors.push(`Duplicate id: ${article.id}`);
    if (seenPaths.has(article.path)) errors.push(`Duplicate path: ${article.path}`);
    if (seenSources.has(article.source)) {
      errors.push(`Duplicate source: ${article.source}`);
    }
    seenIds.add(article.id);
    seenPaths.add(article.path);
    seenSources.add(article.source);

    if (!groupIds.has(article.group)) {
      errors.push(`Unknown group for ${article.id}: ${article.group}`);
    }
    if (!statuses.has(article.status)) {
      errors.push(`Invalid status for ${article.id}: ${article.status}`);
    }
    if (
      article.path !== "/docs" &&
      (!article.path.startsWith("/docs/") || article.path.endsWith("/"))
    ) {
      errors.push(`Invalid docs path for ${article.id}: ${article.path}`);
    }
    if (!/^[a-z0-9][a-z0-9-]*\.md$/.test(article.source)) {
      errors.push(`Invalid source filename for ${article.id}: ${article.source}`);
    }
    if (!isIsoDate(article.lastReviewed)) {
      errors.push(
        `Last reviewed must be an ISO date for ${article.id}: ${article.lastReviewed}`,
      );
    }

    for (const [field, value] of [
      ["navTitle", article.navTitle],
      ["title", article.title],
      ["description", article.description],
      ["appliesTo", article.appliesTo],
    ] as const) {
      if (value.includes("—")) {
        errors.push(`User-visible ${field} contains an em dash for ${article.id}`);
      }
    }
  }

  return errors;
}

export const MIN_PUBLISHED_DOC_WORDS = 120;
export const MIN_PUBLISHED_DOC_H2S = 2;

export function validatePublishedDocContent(
  article: DocArticle,
  markdown: string,
): readonly string[] {
  if (article.status !== "published") return [];

  const errors: string[] = [];
  const words = markdown.match(/[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*/gu) ?? [];
  const h2Count = extractDocHeadings(markdown).filter(
    (heading) => heading.depth === 2,
  ).length;

  if (words.length < MIN_PUBLISHED_DOC_WORDS) {
    errors.push(
      `${article.source} must contain at least ${MIN_PUBLISHED_DOC_WORDS} words when published`,
    );
  }
  if (h2Count < MIN_PUBLISHED_DOC_H2S) {
    errors.push(
      `${article.source} must contain at least ${MIN_PUBLISHED_DOC_H2S} H2 sections when published`,
    );
  }
  if (/documentation in progress/i.test(markdown)) {
    errors.push(`${article.source} still contains placeholder copy`);
  }

  return errors;
}
