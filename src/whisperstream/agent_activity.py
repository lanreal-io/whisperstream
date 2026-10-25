"""Bounded, memory-only retention for visible agent completions.

The store is the canonical handoff between normalized Claude response events
and the HUD completion stream. It deliberately has no persistence or I/O.
Pending response text lives only in process memory and disappears when the
backend exits.

Production limits are explicit:

* at most 100 pending entries, including the optional overflow marker;
* at most 200,000 response characters, including the overflow marker copy.
* provider session IDs up to 256 characters remain exact; oversized IDs are
  retained only as a stable 71-character ``sha256:<hex>`` digest token.

When either limit is exceeded, the oldest unacknowledged responses are
discarded and represented by one acknowledgeable, content-free overflow item.
The limits are configurable only to make boundary behavior cheap to test.

# ============================================================================
# TRANSCRIPT DATA - DO NOT LOG
# ``enqueue`` receives and retains agent response text. Never log response
# content, source/session IDs, upstream turn/prompt IDs, working directories,
# or serialized completion payloads. Metadata-only counts are safe.
# ============================================================================
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Mapping

from .agent_session_names import sanitize_session_name

MAX_PENDING_ITEMS = 100
MAX_PENDING_RESPONSE_CHARS = 200_000
MAX_SESSION_ID_CHARS = 256
DEFAULT_DEDUPE_HISTORY = 400
DEFAULT_SOURCE_HISTORY = 400
MAX_PROJECT_LABEL_CHARS = 48

OVERFLOW_COPY_TEMPLATE = "{count} earlier replies grouped"

_AGENT_LABELS = {"claude": "Claude", "codex": "Codex"}

__all__ = [
    "AgentActivityStore",
    "AgentCompletion",
    "DEFAULT_DEDUPE_HISTORY",
    "DEFAULT_SOURCE_HISTORY",
    "EnqueueResult",
    "MAX_PENDING_ITEMS",
    "MAX_PENDING_RESPONSE_CHARS",
    "MAX_SESSION_ID_CHARS",
    "OVERFLOW_COPY_TEMPLATE",
    "SourceKey",
]


@dataclass(frozen=True, slots=True)
class SourceKey:
    """Semantic agent-session identity, independent from any window or pin.

    Session IDs remain exact through ``MAX_SESSION_ID_CHARS``; longer values
    use the store's fixed-size digest representation.
    """

    agent: str
    session_id: str | None


@dataclass(frozen=True, slots=True)
class AgentCompletion:
    """One immutable response or content-free overflow item."""

    completion_id: str
    sequence: int
    text: str
    source_key: SourceKey | None = None
    source_label: str | None = None
    source_name: str | None = None
    source_name_is_custom: bool = False
    empty: bool = False
    spoken_summary: str | None = None
    overflow_count: int = 0

    @property
    def kind(self) -> str:
        return "overflow" if self.overflow_count else "response"

    @property
    def agent(self) -> str | None:
        return self.source_key.agent if self.source_key is not None else None

    @property
    def session_id(self) -> str | None:
        return self.source_key.session_id if self.source_key is not None else None

    def to_dict(self) -> dict:
        """Return a detached JSON-ready payload without transport metadata.

        Semantic session/source keys intentionally remain store-internal; the
        HUD wire contract needs only the agent and privacy-safe display label.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # The returned ``text`` field contains response content for normal
        # completions. Do not log this mapping or interpolate it into errors.
        # ====================================================================
        """
        payload = {
            "completion_id": self.completion_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "text": self.text,
        }
        if self.kind == "overflow":
            payload["overflow_count"] = self.overflow_count
            return payload

        assert self.source_key is not None
        payload.update(
            {
                "agent": self.source_key.agent,
                "source_label": self.source_label,
                "empty": self.empty,
            }
        )
        if self.source_name is not None:
            payload["source_name"] = self.source_name
            payload["source_name_is_custom"] = self.source_name_is_custom
        if self.spoken_summary is not None:
            payload["spoken_summary"] = self.spoken_summary
        return payload


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Post-enforcement entries that a downstream consumer should upsert.

    ``changed`` contains only entries that are still pending after this call,
    ordered by sequence. An ordinary insertion returns that completion. A
    limit-triggered insertion returns the created/updated overflow marker and,
    only when retained, the new completion. Existing unaffected entries are
    not replayed. A provider retry returns ``changed=()`` and
    ``duplicate=True``.
    """

    changed: tuple[AgentCompletion, ...]
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class _SourceLabelRecord:
    base_label: str
    display_label: str


class AgentActivityStore:
    """Thread-safe FIFO store for unacknowledged visual completions.

    ``enqueue`` consumes the normalized response mapping produced by
    ``parse_claude_event``. It returns only newly
    changed entries that remain pending after limits are enforced, ready for a
    downstream upsert/broadcast loop. A retry carrying the same provider
    prompt/turn identity and response text returns an empty result, including
    shortly after acknowledgement or overflow eviction. Distinct responses in
    one continued prompt chain remain distinct completions. Events without an
    upstream identity are never content-deduplicated because equal text can be
    two legitimate replies.

    Upstream identities and the response text used to distinguish them are
    retained only as one fixed-size, memory-only SHA-256 digest. Session IDs
    longer than ``MAX_SESSION_ID_CHARS`` are likewise replaced by a stable
    fixed-size digest token before a completion or source record can retain
    them. The working directory is reduced to a sanitized basename before the
    lock is released and is never stored. Recent dedupe and source-label
    registries are bounded separately so acknowledged sessions cannot grow
    memory forever.
    """

    def __init__(
        self,
        *,
        max_items: int = MAX_PENDING_ITEMS,
        max_response_chars: int = MAX_PENDING_RESPONSE_CHARS,
        dedupe_history: int = DEFAULT_DEDUPE_HISTORY,
        source_history: int = DEFAULT_SOURCE_HISTORY,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        if (
            not isinstance(max_response_chars, int)
            or isinstance(max_response_chars, bool)
            or max_response_chars < len(OVERFLOW_COPY_TEMPLATE.format(count=1))
        ):
            raise ValueError("max_response_chars must fit the overflow copy")
        if (
            not isinstance(dedupe_history, int)
            or isinstance(dedupe_history, bool)
            or dedupe_history < 0
        ):
            raise ValueError("dedupe_history must be a non-negative integer")
        if (
            not isinstance(source_history, int)
            or isinstance(source_history, bool)
            or source_history < max_items
        ):
            raise ValueError("source_history must be an integer at least max_items")

        self._max_items = max_items
        self._max_response_chars = max_response_chars
        self._dedupe_history_limit = dedupe_history
        self._source_history_limit = source_history
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

        self._lock = threading.Lock()
        self._next_sequence = 1
        self._items: "OrderedDict[str, AgentCompletion]" = OrderedDict()
        self._response_chars = 0
        self._overflow: AgentCompletion | None = None

        # Pending identities must remain deduplicated for as long as their
        # completion can be acknowledged. Removed identities move into the
        # bounded recent set so a transport retry does not resurrect an acked
        # or grouped response.
        self._pending_dedupe: dict[str, str] = {}
        self._completion_dedupe: dict[str, str] = {}
        self._recent_dedupe: "OrderedDict[str, None]" = OrderedDict()

        # Source records hold only bounded semantic IDs plus privacy-safe
        # labels. No working-directory path is retained.
        self._source_labels: "OrderedDict[SourceKey, _SourceLabelRecord]" = (
            OrderedDict()
        )

    def enqueue(self, event: Mapping[str, object]) -> EnqueueResult:
        """Retain a response and return only pending entries changed by the call.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # ``event['text']`` is agent response content. This method performs no
        # logging. Do not add event, text, path, or identity values to logs.
        # ====================================================================
        """
        normalized = self._validate_event(event)
        (
            agent,
            session_id,
            text,
            empty,
            project_name,
            custom_name,
            custom_is_explicit,
            spoken_summary,
            upstream_digest,
        ) = normalized

        source_name, source_name_is_custom = _resolve_source_name(
            agent, custom_name, custom_is_explicit, project_name
        )

        with self._lock:
            if upstream_digest is not None:
                if upstream_digest in self._pending_dedupe:
                    return EnqueueResult(changed=(), duplicate=True)
                if upstream_digest in self._recent_dedupe:
                    self._recent_dedupe.move_to_end(upstream_digest)
                    return EnqueueResult(changed=(), duplicate=True)

            overflow_before = self._overflow
            source_key = SourceKey(agent, session_id)
            source_label = self._source_label_for(source_key, project_name).display_label
            sequence = self._next_sequence
            self._next_sequence += 1
            completion = AgentCompletion(
                completion_id=self._new_completion_id(),
                sequence=sequence,
                text=text,
                source_key=source_key,
                source_label=source_label,
                source_name=source_name,
                source_name_is_custom=source_name_is_custom,
                empty=empty,
                spoken_summary=spoken_summary,
            )
            self._items[completion.completion_id] = completion
            self._response_chars += len(text)

            if upstream_digest is not None:
                self._pending_dedupe[upstream_digest] = completion.completion_id
                self._completion_dedupe[completion.completion_id] = upstream_digest

            self._enforce_limits()
            self._prune_source_labels()

            changed: list[AgentCompletion] = []
            if self._overflow != overflow_before:
                assert self._overflow is not None
                changed.append(self._overflow)
            retained = self._items.get(completion.completion_id)
            if retained is not None:
                changed.append(retained)
            changed.sort(key=lambda item: item.sequence)
            return EnqueueResult(changed=tuple(changed))

    def snapshot(self) -> tuple[AgentCompletion, ...]:
        """Return an immutable FIFO snapshot of all pending entries."""
        with self._lock:
            if self._overflow is None:
                return tuple(self._items.values())
            return (self._overflow, *self._items.values())

    def acknowledge(self, completion_id: str) -> bool:
        """Remove one pending entry; repeated or unknown acknowledgements are no-ops."""
        if not isinstance(completion_id, str) or not completion_id:
            return False
        with self._lock:
            if (
                self._overflow is not None
                and completion_id == self._overflow.completion_id
            ):
                self._overflow = None
                return True

            completion = self._items.pop(completion_id, None)
            if completion is None:
                return False
            self._response_chars -= len(completion.text)
            self._retire_dedupe(completion_id)
            self._prune_source_labels()
            return True

    def clear(self) -> None:
        """Drop every retained completion, dedupe identity, and source label.

        Feature disable, Lockdown entry, shutdown, and the dev HUD simulator
        call this seam. Content dedupe is what makes a real transport retry
        safe, and the simulator also needs a pristine store between scenarios.
        """
        with self._lock:
            self._next_sequence = 1
            self._items.clear()
            self._response_chars = 0
            self._overflow = None
            self._pending_dedupe.clear()
            self._completion_dedupe.clear()
            self._recent_dedupe.clear()
            self._source_labels.clear()

    def stats(self) -> dict[str, int]:
        """Return content-free bounded-store accounting for diagnostics/tests."""
        with self._lock:
            return {
                "pending_items": len(self._items) + int(self._overflow is not None),
                "pending_response_chars": self._pending_chars(),
                "overflow_count": (
                    self._overflow.overflow_count if self._overflow is not None else 0
                ),
                "recent_dedup_identities": len(self._recent_dedupe),
                "source_labels": len(self._source_labels),
            }

    @staticmethod
    def _validate_event(
        event: Mapping[str, object],
    ) -> tuple[
        str,
        str | None,
        str,
        bool,
        str | None,
        str | None,
        bool,
        str | None,
        str | None,
    ]:
        """Validate metadata and reduce sensitive fields before retention.

        # ====================================================================
        # TRANSCRIPT DATA - DO NOT LOG
        # This function reads response text, upstream IDs, and cwd. Return only
        # the exact text, semantic source fields, a basename, a sanitized
        # session name, and one memory-only response-identity digest.
        # ====================================================================
        """
        if not isinstance(event, Mapping):
            raise TypeError("event must be a mapping")
        if event.get("kind") != "response":
            raise ValueError("only normalized response events can be enqueued")

        agent_value = event.get("agent")
        if agent_value not in _AGENT_LABELS:
            raise ValueError("agent must be claude or codex")

        text_value = event.get("text")
        if not isinstance(text_value, str):
            raise TypeError("response text must be a string")

        session_id = _bounded_session_id(event.get("session_id"))

        empty_value = event.get("empty")
        if empty_value is None:
            empty = not text_value.strip()
        elif isinstance(empty_value, bool):
            empty = empty_value
        else:
            raise TypeError("empty must be a boolean when provided")

        cwd_value = event.get("cwd")
        project_name = _project_basename(cwd_value if isinstance(cwd_value, str) else None)

        # Step 1 of the session-name resolution chain. A candidate that does
        # not survive sanitization simply falls through to the project
        # basename, so an unusable name is never an error.
        custom_name = sanitize_session_name(event.get("source_name"))
        custom_flag_value = event.get("source_name_is_custom")
        if custom_flag_value is None:
            custom_is_explicit = True
        elif isinstance(custom_flag_value, bool):
            custom_is_explicit = custom_flag_value
        else:
            raise TypeError("source_name_is_custom must be a boolean when provided")

        spoken_summary_value = event.get("spoken_summary")
        spoken_summary = (
            spoken_summary_value.strip()
            if isinstance(spoken_summary_value, str) and spoken_summary_value.strip()
            else None
        )

        identity_field = "turn_id" if agent_value == "codex" else "prompt_id"
        identity_value = event.get(identity_field)
        upstream_digest = None
        if isinstance(identity_value, str) and identity_value:
            digest_input = "\0".join(
                (
                    agent_value,
                    session_id or "",
                    identity_field,
                    identity_value,
                    text_value,
                )
            )
            upstream_digest = hashlib.sha256(
                digest_input.encode("utf-8", errors="surrogatepass")
            ).hexdigest()

        return (
            agent_value,
            session_id,
            text_value,
            empty,
            project_name,
            custom_name,
            custom_is_explicit,
            spoken_summary,
            upstream_digest,
        )

    def _source_label_for(
        self, source_key: SourceKey, project_name: str | None
    ) -> _SourceLabelRecord:
        """Return the source's label record so one pass serves label and name.

        ``display_label`` carries the collision suffix and feeds
        ``source_label`` verbatim. The resolved ``source_name`` is derived
        separately by :func:`_resolve_source_name` and is deliberately never
        suffixed: a name is an identity a human chose, not a uniqueness token.
        """
        label = _AGENT_LABELS[source_key.agent]
        base_label = f"{label} · {project_name}" if project_name else f"{label} session"

        # A missing provider session ID cannot be stably distinguished. Avoid
        # pretending cwd is a conversation identity and derive the safe base
        # label independently for each such event.
        if source_key.session_id is None:
            return _SourceLabelRecord(base_label=base_label, display_label=base_label)

        active_sources = self._active_source_keys()
        labels_in_use = {
            self._source_labels[key].display_label
            for key in active_sources
            if key != source_key and key in self._source_labels
        }

        existing = self._source_labels.get(source_key)
        if existing is not None and existing.display_label not in labels_in_use:
            self._source_labels.move_to_end(source_key)
            return existing

        if existing is not None:
            # Preserve the source's original human label when it recurs. Only
            # its collision suffix may need to change if another live source
            # currently reserves the old display label.
            base_label = existing.base_label

        collides = any(
            self._source_labels[key].base_label == base_label
            for key in active_sources
            if key != source_key and key in self._source_labels
        )
        display_label = base_label
        if collides:
            display_label = self._label_with_unique_suffix(
                base_label, labels_in_use
            )

        record = _SourceLabelRecord(
            base_label=base_label,
            display_label=display_label,
        )
        self._source_labels[source_key] = record
        return record

    def _label_with_unique_suffix(
        self,
        base_label: str,
        labels_in_use: set[str],
    ) -> str:
        """Choose a short-lived display ordinal, never a session fingerprint."""

        for ordinal in range(2, self._source_history_limit + 2):
            candidate = f"{base_label} · {ordinal}"
            if candidate not in labels_in_use:
                return candidate
        return f"{base_label} · {self._source_history_limit + 2}"

    def _new_completion_id(self) -> str:
        completion_id = self._id_factory()
        if not isinstance(completion_id, str) or not completion_id:
            raise ValueError("id_factory must return a non-empty string")
        if completion_id in self._items or (
            self._overflow is not None
            and completion_id == self._overflow.completion_id
        ):
            raise ValueError("id_factory returned a duplicate pending ID")
        return completion_id

    def _enforce_limits(self) -> None:
        while (
            len(self._items) + int(self._overflow is not None) > self._max_items
            or self._pending_chars() > self._max_response_chars
        ):
            if not self._items:
                # The configured minimum always fits a one-digit marker. This
                # guard also makes the loop total for unusual test-scale limits
                # after an astronomically large overflow count.
                break
            _, dropped = self._items.popitem(last=False)
            self._response_chars -= len(dropped.text)
            self._retire_dedupe(dropped.completion_id)
            self._group_dropped(dropped.sequence)

    def _group_dropped(self, dropped_sequence: int) -> None:
        if self._overflow is None:
            count = 1
            self._overflow = AgentCompletion(
                completion_id=self._new_completion_id(),
                sequence=dropped_sequence,
                text=OVERFLOW_COPY_TEMPLATE.format(count=count),
                overflow_count=count,
            )
            return

        count = self._overflow.overflow_count + 1
        self._overflow = AgentCompletion(
            completion_id=self._overflow.completion_id,
            sequence=min(self._overflow.sequence, dropped_sequence),
            text=OVERFLOW_COPY_TEMPLATE.format(count=count),
            overflow_count=count,
        )

    def _pending_chars(self) -> int:
        overflow_chars = len(self._overflow.text) if self._overflow is not None else 0
        return self._response_chars + overflow_chars

    def _retire_dedupe(self, completion_id: str) -> None:
        digest = self._completion_dedupe.pop(completion_id, None)
        if digest is None:
            return
        self._pending_dedupe.pop(digest, None)
        if self._dedupe_history_limit == 0:
            return
        self._recent_dedupe[digest] = None
        self._recent_dedupe.move_to_end(digest)
        while len(self._recent_dedupe) > self._dedupe_history_limit:
            self._recent_dedupe.popitem(last=False)

    def _prune_source_labels(self) -> None:
        if len(self._source_labels) <= self._source_history_limit:
            return

        active_sources = self._active_source_keys()
        while len(self._source_labels) > self._source_history_limit:
            removable = next(
                (key for key in self._source_labels if key not in active_sources),
                None,
            )
            if removable is None:
                break
            del self._source_labels[removable]

    def _active_source_keys(self) -> set[SourceKey]:
        """Return semantic sources with at least one pending completion."""
        return {
            item.source_key
            for item in self._items.values()
            if item.source_key is not None and item.source_key.session_id is not None
        }


def _bounded_session_id(value: object) -> str | None:
    """Keep provider-sized IDs exact and digest oversized metadata before retention."""
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= MAX_SESSION_ID_CHARS:
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"sha256:{digest}"


def _resolve_source_name(
    agent: str,
    custom_name: str | None,
    custom_is_explicit: bool,
    project_name: str | None,
) -> tuple[str, bool]:
    """Resolve the display session name and whether a human chose it.

    The chain is: a supplied (already sanitized) custom name, then the project
    basename, then the literal agent label. The terminal value is always a
    usable string so the frontend never has to invent one. Only the first rung
    reports ``is_custom``; the basename is a disambiguator the user did not
    choose and must never be spoken.

    # ========================================================================
    # TRANSCRIPT DATA - DO NOT LOG
    # The returned name is transcript-class. Log ``named=true|false`` at most.
    # ========================================================================
    """
    if custom_name is not None:
        return custom_name, bool(custom_is_explicit)
    basename = sanitize_session_name(project_name)
    if basename is not None:
        return basename, False
    return _AGENT_LABELS[agent], False


def _project_basename(cwd: str | None) -> str | None:
    """Derive one sanitized path component without resolving or storing cwd."""
    if not cwd or not cwd.strip():
        return None
    stripped = cwd.strip()
    windows_path = PureWindowsPath(stripped)
    if "\\" in stripped or windows_path.drive:
        name = windows_path.name
    else:
        name = PurePosixPath(stripped).name
    if not name or name in {".", "..", "/", "\\"}:
        return None

    printable = "".join(
        character if character.isprintable() and character not in "/\\" else " "
        for character in name
    )
    compact = " ".join(printable.split())
    if not compact:
        return None
    return compact[:MAX_PROJECT_LABEL_CHARS]
