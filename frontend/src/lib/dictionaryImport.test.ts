import { describe, expect, it } from "vitest";
import { parseVocabularyCsv, reviewVocabulary, vocabularyImportPatch, VOCABULARY_EXAMPLE } from "./dictionaryImport";

const empty = { word_boosts: [], word_filters: [], word_replacements: {} };
describe("vocabulary CSV import", () => {
  it("accepts boost-only headers, BOM, case, Windows lines, and multiword names", () => {
    expect(parseVocabularyCsv('\uFEFF WORD\r\nTanStack\r\n"Acme, Inc."\r\n').rows).toEqual([
      { word: "TanStack", replacement: "", row: 2 }, { word: "Acme, Inc.", replacement: "", row: 3 },
    ]);
  });
  it("supports either column order, quoted newlines and escaped quotes", () => {
    expect(parseVocabularyCsv('replacement,word\n"Philip ""Phil""",Filip\n,"A\nB"').rows).toEqual([
      { word: "Filip", replacement: 'Philip "Phil"', row: 2 }, { word: "A\nB", replacement: "", row: 3 },
    ]);
  });
  it.each(["", "\n \r\n", "word", "word\n", "word,replacement\n"])('rejects empty/header-only files: %j', text => {
    expect(() => parseVocabularyCsv(text)).toThrow("This file has no entries");
  });
  it.each([
    ["replacement\nPhilip", "Add a word header"],
    ["word,word\nOne,Two", "Each column header"],
    ["word,extra\nOne,Two", "Use only word"],
    ['word\n"unfinished', "Couldn't read this CSV"],
    ['word\n"closed"oops', "Couldn't read this CSV"],
  ])("reports recoverable schema errors", (text, message) => {
    expect(() => parseVocabularyCsv(text)).toThrow(message);
  });
  it("blocks missing words and mismatched records without silently importing the good rows", () => {
    const file = parseVocabularyCsv("word,replacement\nGood,\n,Philip\nExtra,a,b");
    expect(file.rows).toHaveLength(1);
    expect(file.invalid).toEqual(["Row 3: Add a word or phrase.", "Row 4: Use one value for each column in the header."]);
    expect(() => vocabularyImportPatch(file, empty)).toThrow("Fix these rows");
  });
  it("deduplicates locally and against all current entry types without overwrites", () => {
    const config = { word_boosts: ["TanStack", "Filip"], word_filters: ["um"], word_replacements: { Filip: "Philip" } };
    const file = parseVocabularyCsv("word,replacement\ntanstack,\nFilip,Philip\num,\nNewName,\nnewname,\nNewName,Other\nFilip,Other");
    const review = reviewVocabulary(file, config);
    expect(review.additions.map(row => row.word)).toEqual(["NewName"]);
    expect(review.duplicates).toHaveLength(3);
    expect(review.conflicts).toHaveLength(3);
    expect(vocabularyImportPatch(file, config)).toEqual({ word_boosts: ["NewName", "TanStack", "Filip"], word_replacements: { Filip: "Philip" } });
  });
  it("rechecks the latest config at commit and protects property-like replacement sources", () => {
    const file = parseVocabularyCsv("word,replacement\nTanStack,\n__proto__,prototype");
    const patch = vocabularyImportPatch(file, { ...empty, word_boosts: ["TanStack"] });
    expect(patch.word_boosts).toEqual(["TanStack"]);
    expect(Object.entries(patch.word_replacements!)).toEqual([["__proto__", "prototype"]]);
    expect(Object.getPrototypeOf(patch.word_replacements)).toBe(Object.prototype);
  });
  it("ships an example that is valid under the same importer", () => {
    expect(reviewVocabulary(parseVocabularyCsv(VOCABULARY_EXAMPLE), empty).additions).toHaveLength(3);
  });
});
