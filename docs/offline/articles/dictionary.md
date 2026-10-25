## Correct recurring words

Use **Dictionary** to remove unwanted words, replace recognized wording, or help AI enhancement spell names and specialist terms. Each entry applies automatically to later dictations until you edit or remove it.

| Entry | Result | Example |
|---|---|---|
| **Boost** | Suggests a spelling to AI enhancement when a similar word or sound appears | Keep `TanStack` spelled correctly when it is heard as `tan stack` |
| **Filter** | Removes a matched word or phrase | Remove `um` from `Send it, um, tomorrow.` |
| **Replace** | Substitutes your chosen text for a match | Change `whisper stream` to `Whisperstream` |

A new install starts with an empty Dictionary. Add only the entries that suit the way you speak. Pressing Enter in **Add a word...** adds a Filter, so select **Boost** or **Replace** when you want those instead.

## Boost names and terms

Use Boost for a name, product, or technical term that recognition sometimes misspells or confuses with a similar-sounding word.

1. Open **Dictionary**.
2. Enter the correct spelling in **Add a word...**.
3. Select **Boost**.
4. Make a new dictation with AI enhancement on to test the result.

Boost requires AI enhancement and an enhancement model that supports it. Cloud providers, Ollama, Custom, Stream-1 Mini, and Customizable support Boost. When enhancement is off or the selected model does not support it, Dictionary shows a notice and keeps your saved words.

For each dictation, Whisperstream finds Boost words that look or sound like parts of the transcript and gives them to the enhancement model as spelling hints. The model decides whether a hint fits. Boost does not force a replacement, so use **Replace** when a wording should always change. Very short entries match only exact wording, and long phrases are not used as hints. If you use a cloud provider, the matching Boost words are sent with the transcript.

## Add a filter

Use a filter when a word or phrase should disappear from completed text.

1. Open **Dictionary**.
2. Enter the unwanted wording in **Add a word...**.
3. Select **Filter**.
4. Make a new dictation to test the result.

Filters match complete words or phrases regardless of capitalization. A filter for `um` removes `um` and `Um`, but it does not remove those letters from the middle of another word. Whisperstream also cleans up extra horizontal spaces and nearby commas left by the removal.

| Recognized text | Filter | Result |
|---|---|---|
| `Send it, um, tomorrow.` | `um` | `Send it, tomorrow.` |
| `Uh please open the report.` | `uh` | `please open the report.` |

Use filters narrowly. Filtering an ordinary content word removes it every time it appears as a complete match, even when the word carries meaning in that sentence.

## Add a replacement

Use a replacement when recognition is consistent but the spelling, capitalization, or wording should be different.

1. Open **Dictionary**.
2. Enter the recognized wording in **Add a word...**.
3. Select **Replace**.
4. Enter the desired result in **Replace with**.
5. Select **Save**, then test it in a new dictation.

Replacements match complete words or phrases regardless of capitalization and use the spelling and capitalization you saved. Other text-style settings can still affect the completed result.

| Recognized text | Replace with | Result |
|---|---|---|
| `whisper stream` | `Whisperstream` | `Whisperstream` |
| `acme cloud` | `ACME Cloud` | `ACME Cloud` |
| `doctor lee` | `Dr. Lee` | `Dr. Lee` |

Use the source wording Whisperstream produces reliably. If a name is recognized in several forms, add one specific replacement for each form instead of one broad rule that could change unrelated text.

## Understand matching order

When entries overlap, Whisperstream checks longer source phrases before shorter ones. For example, `acme cloud` is checked before `acme`.

Filters run before replacements. If the same wording appears in both entry types, the filter removes it before the replacement can apply.

## Edit or remove an entry

Select an entry's edit control, change its source or replacement, and save. You can also press Enter to save or Escape to cancel while editing. Select the remove control to delete the entry. Changes apply to future completed dictations.

If a rule does not match, compare it with the exact wording in a new completed dictation. Check spelling and spaces, and remember that the match must begin and end on word boundaries.

## Import a word list

Add many Boost words and replacements at once from a CSV file.

1. Open **Dictionary** and select **Import CSV**.
2. Drop a CSV file onto the import area, or select **Choose file**. Select **Download example CSV** to start from a sample.
3. Review the entries under **Ready to add**.
4. Select the **Import** button, which shows how many entries will be added.

The file needs a `word` column. A `replacement` column is optional. A row with a replacement becomes a Replace entry; a row without one becomes a Boost entry. Filters cannot be imported.

```text
word,replacement
Whisperstream,
TanStack,
git hub,GitHub
```

Import never changes existing entries. Duplicates of entries you already have, and words that already have a different rule, are skipped and listed in the review. If any row has a problem, fix it in the CSV and choose the file again. Closing the import before you confirm adds nothing.
