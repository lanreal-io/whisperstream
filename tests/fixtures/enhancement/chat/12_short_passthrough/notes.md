Single-word reply. Tests two behaviors:
1. Parakeet's real output ("Sure." — capitalized with period) is preserved verbatim.
2. The ≤ 3-word skip logic in `llm_enhancer.py` engages, so the LLM is never called for this input. Passthrough is enforced in code, not in the prompt.

If the skip threshold is ever lowered below 1 or removed, this fixture would still pass as long as the LLM doesn't damage clean single-word output — but the cost-savings intent would be lost.
