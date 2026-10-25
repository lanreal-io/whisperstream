import type { AppConfig } from "./api/config";

export type DictionaryImportConfig = Pick<AppConfig, "word_boosts" | "word_filters" | "word_replacements">;
export interface VocabularyRow { word: string; replacement: string; row: number }
export interface VocabularyFile { rows: VocabularyRow[]; invalid: string[] }
export interface VocabularyReview extends VocabularyFile {
  additions: VocabularyRow[];
  duplicates: VocabularyRow[];
  conflicts: VocabularyRow[];
}

export const VOCABULARY_EXAMPLE = "word,replacement\r\nWhisperstream,\r\nTanStack,\r\ngit hub,GitHub\r\n";
export const normalizeDictionaryWord = (word: string) => word.normalize("NFKC").trim().replace(/\s+/g, " ").toLowerCase();
const FORMAT_ERROR = "Couldn't read this CSV. Check its format or start with the example file.";

/** CSV data stays local. Never log its content, filenames, or native paths. */
export function parseVocabularyCsv(text: string): VocabularyFile {
  const records: string[][] = [];
  let record: string[] = [], cell = "", quoted = false, afterQuote = false;
  text = text.replace(/^\uFEFF/, "");
  for (let i = 0; i < text.length; i++) {
    const character = text[i];
    if (quoted) {
      if (character === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (character === '"') { quoted = false; afterQuote = true; }
      else cell += character;
      continue;
    }
    if (character === '"') {
      if (cell || afterQuote) throw new Error(FORMAT_ERROR);
      quoted = true;
    } else if (character === ",") {
      record.push(cell); cell = ""; afterQuote = false;
    } else if (character === "\n" || character === "\r") {
      if (character === "\r" && text[i + 1] === "\n") i++;
      record.push(cell); records.push(record); record = []; cell = ""; afterQuote = false;
    } else if (afterQuote) {
      if (character !== " " && character !== "\t") throw new Error(FORMAT_ERROR);
    } else cell += character;
  }
  if (quoted) throw new Error(FORMAT_ERROR);
  if (record.length || cell || afterQuote) { record.push(cell); records.push(record); }
  const emptyMessage = "This file has no entries. Add a word below the headers, then try again.";
  if (!records.length || records.every(row => row.every(value => !value.trim()))) throw new Error(emptyMessage);
  const headers = records.shift()!.map(normalizeDictionaryWord);
  if (!headers.includes("word")) throw new Error("Add a word header to the first row, then try again.");
  if (new Set(headers).size !== headers.length) throw new Error("Each column header must appear once. Remove repeated columns, then try again.");
  if (headers.length > 2 || headers.some(header => header !== "word" && header !== "replacement")) {
    throw new Error("Use only word and optional replacement columns. Remove other columns, then try again.");
  }
  const wordIndex = headers.indexOf("word"), replacementIndex = headers.indexOf("replacement");
  const result: VocabularyFile = { rows: [], invalid: [] };
  records.forEach((values, index) => {
    if (values.every(value => !value.trim())) return;
    const row = index + 2;
    if (values.length !== headers.length) {
      result.invalid.push(`Row ${row}: Use one value for each column in the header.`);
      return;
    }
    const word = values[wordIndex].trim();
    if (!word) { result.invalid.push(`Row ${row}: Add a word or phrase.`); return; }
    result.rows.push({ word, replacement: replacementIndex < 0 ? "" : values[replacementIndex].trim(), row });
  });
  if (!result.rows.length && !result.invalid.length) throw new Error(emptyMessage);
  return result;
}

/** Keep every existing rule, including sources with more than one action. */
export function reviewVocabulary(file: VocabularyFile, config: DictionaryImportConfig): VocabularyReview {
  const seen = new Map<string, Set<string>>();
  const add = (word: string, action: string) => {
    const key = normalizeDictionaryWord(word);
    const actions = seen.get(key) ?? new Set<string>();
    actions.add(action); seen.set(key, actions);
  };
  config.word_boosts.forEach(word => add(word, "boost"));
  config.word_filters.forEach(word => add(word, "filter"));
  Object.entries(config.word_replacements).forEach(([word, replacement]) => add(word, `replace:${replacement}`));
  const result: VocabularyReview = { ...file, additions: [], duplicates: [], conflicts: [] };
  file.rows.forEach(row => {
    const action = row.replacement ? `replace:${row.replacement}` : "boost";
    const existing = seen.get(normalizeDictionaryWord(row.word));
    if (existing?.has(action)) result.duplicates.push(row);
    else if (existing) result.conflicts.push(row);
    else { result.additions.push(row); add(row.word, action); }
  });
  return result;
}

/** Call against the latest config at commit, not a snapshot from file selection. */
export function vocabularyImportPatch(file: VocabularyFile, config: DictionaryImportConfig): Partial<AppConfig> {
  const review = reviewVocabulary(file, config);
  if (review.invalid.length) throw new Error("Fix these rows in your CSV, then choose the file again.");
  return {
    word_boosts: [...review.additions.filter(row => !row.replacement).map(row => row.word), ...config.word_boosts],
    word_replacements: Object.fromEntries([
      ...review.additions.filter(row => row.replacement).map(row => [row.word.toLowerCase(), row.replacement]),
      ...Object.entries(config.word_replacements),
    ]),
  };
}
