Two LLM transforms under test:
1. Collapse repeated word: "that's that's" → "that's"
2. Recompose mangled slang acronym: Parakeet tokenizes "ngl" as "Ng L." (treating it as two letters plus a sentence-end period because the slang isn't in its vocab). The LLM should recognize this pattern and produce "ngl".

Caps note: "Lol" is left capitalized because Parakeet outputs it sentence-initially with caps. Strict slang convention is lowercase, but preserving Parakeet's caps is the simpler rule. If the prompt later adopts a "lowercase slang at sentence start" rule, update this fixture's expected.
