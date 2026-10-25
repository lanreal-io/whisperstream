Time-fabrication guard: a part-of-day reference with NO spoken hour ("Thursday afternoon") must stay as words. The cleaner must NOT invent a clock time ("2 PM", "2:00").

The input is already clean, so the correct output is unchanged (minimal-edit). This is the negative counterpart to `18_time_spoken` (hour + signal → "9:30 AM") and reinforces `17_date_spoken` (which already requires "noon" to stay "noon").

Origin: the local-finetune prompt-enrichment sweep observed the cleaner regress "noon" → "12 PM" (chat/17) and invent "6:45 AM" from an unspoken AM (GAP 2, rt26) when irrelevant time rules perturbed it. The CHAT_PROMPT now states: only map a part-of-day to AM/PM when an hour is ALSO spoken; a bare part-of-day stays a word.
