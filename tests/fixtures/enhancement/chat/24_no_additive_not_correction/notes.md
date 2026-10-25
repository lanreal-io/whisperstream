Content-preservation guard: "X, no Y" is NOT always a self-correction. Here "no leftovers" is additive content (dinner was great AND there were none left), not a correction of "great".

The bare-"no" self-correction rule ("Tuesday, no Wednesday" → "Wednesday", see `08_correction_multiple`) must NOT fire here. The failure mode is dropping the first clause ("Dinner was leftovers.") or dropping "no leftovers". Both clauses must survive.

Origin: GAP 1 (over-trim of voiced words) from the local-finetune gap audit. The CHAT_PROMPT now adds: "NEVER drop content the speaker actually said, even if it seems redundant or could be inferred. Keep every distinct clause the speaker voiced." (canonical example: "the contract came back clean, no redlines" keeps "no redlines").
