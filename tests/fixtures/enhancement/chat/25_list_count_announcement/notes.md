List lead-in rule, drop-the-count-announcement half: a bare count-announcement that only states how many items follow ("a couple of things", "three things for the launch") IS itself a cue and must be dropped. With no real lead-in clause left, emit just the numbered items, and strip the order-cue words ("first", "second").

Contrast with `14_list_numbered_explicit`, where a real lead-in ("Here's what I need to grab:") is KEPT with a colon. The distinction: "Here's the plan:" is a lead-in to keep; "a couple of things" is a count-announcement to drop.

Preserve voice/capitalization per `15_list_first_second_third`: capitalize the first letter of each item, no trailing period on short verb/noun phrases.

Origin: the local-finetune prompt-enrichment sweep flagged list-cue handling (items losing voice, e.g. chat/15 dropping "I'll"). The CHAT_PROMPT now spells out the keep-lead-in vs drop-count-announcement split explicitly.
