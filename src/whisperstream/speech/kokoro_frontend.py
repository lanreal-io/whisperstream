"""Owned, eSpeak-free English frontend for Kokoro.

The lexicon/context behavior in this module is a narrow adaptation of Misaki's
Apache-2.0 English frontend at commit
``fba1236595f2d2bf21d414ba6e57d25256afada3``. Whisperstream replaces Misaki's
spaCy, Torch/Transformers, regex, addict, and num2words dependencies with the
small implementations below and :mod:`number_normalizer`.

The :class:`AveragedPerceptronTagger` feature templates and greedy inference
behavior are a modified, inference-only port of NLTK's MIT-licensed tagger:
original implementation Copyright 2013 Matthew Honnibal; NLTK modifications
Copyright 2015 The NLTK Project; NLTK port author Long Duong. The exact MIT
notice is retained in ``vendor/kokoro/NLTK-PERCEPTRON-MIT.txt`` and in the
optional model archive.

Large lexicons, POS weights, the Kokoro vocabulary, and neural G2P graph are
loaded from the optional Kokoro model directory. None are source-bundled.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence
import unicodedata

from .engine_specs import SpeechInputError
from .number_normalizer import NumberContext, expand_number


_TAGGER_STEM = "averaged_perceptron_tagger_eng"
_PUNCTUATION = frozenset(';:,.!?"“”()—…')
_NON_QUOTE_PUNCTUATION = _PUNCTUATION - frozenset('"“”')
_VOWELS = frozenset("AIOQWYaiuæɐɑɒɔəɛɜɪʊʌᵻ")
_CONSONANTS = frozenset("bdfhjklmnpstvwzðŋɡɹɾʃʒʤʧθ")
_STRESSES = frozenset("ˈˌ")
_IMPERATIVE_HOMOGRAPHS = frozenset(
    {"bow", "close", "object", "present", "project", "record", "refuse", "tear", "wind"}
)
_CURRENCY_SYMBOLS = frozenset({"$", "£", "€"})
_SILENT_JUNKS = frozenset({"-", "_"})
_SYMBOL_WORDS = {
    "%": "percent",
    "&": "and",
    "+": "plus",
    "@": "at",
    "/": "slash",
}
_INLINE_OVERRIDE = re.compile(
    r"\[(?P<label>[^\]]+)\]\(/(?P<phonemes>[^)]*?)/\)",
    re.UNICODE,
)
_TOKEN_RE = re.compile(
    r"""
    (?P<override>\[[^\]]+\]\(/[^)]*?/\))
    |(?P<number>[-+]?\d+(?:(?:[.,:_-])\d+)*(?:st|nd|rd|th|ing|'d|'s|ed|s)?)
    |(?P<word>[^\W\d_]+(?:['’][^\W\d_]+)*)
    |(?P<symbol>[^\s])
    """,
    re.IGNORECASE | re.UNICODE | re.VERBOSE,
)
_IDENTIFIER_PARTS = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+",
)


class FrontendDataError(RuntimeError):
    """The optional frontend data is absent, malformed, or incomplete."""


class UnpronounceableTextError(SpeechInputError):
    """One input cannot be pronounced with the reviewed English frontend."""


class InvalidPhonemeError(UnpronounceableTextError):
    """A pronunciation contains a token unknown to the Kokoro vocabulary."""


class G2PAdapter(Protocol):
    """Narrow fallback contract used by the English frontend."""

    def phonemize(self, word: str) -> str:
        """Return Kokoro-compatible phonemes for one unknown token."""


@dataclass
class _Token:
    text: str
    whitespace: str
    tag: str = ""
    phonemes: str | None = None
    sentence_start: bool = False


@dataclass(frozen=True)
class _TokenContext:
    future_vowel: bool | None = None
    future_to: bool = False


class AveragedPerceptronTagger:
    """Minimal inference-only reader for NLTK's MIT-labelled JSON tagger data."""

    _START = ("-START-", "-START2-")
    _END = ("-END-", "-END2-")

    def __init__(
        self,
        *,
        weights: Mapping[str, Mapping[str, float]],
        classes: Sequence[str],
        tagdict: Mapping[str, str],
    ) -> None:
        if not classes:
            raise FrontendDataError("POS tagger classes are empty")
        self._weights = {
            feature: {label: float(weight) for label, weight in labels.items()}
            for feature, labels in weights.items()
        }
        self._classes = tuple(str(label) for label in classes)
        self._tagdict = {str(word): str(tag) for word, tag in tagdict.items()}

    @classmethod
    def from_model_dir(cls, model_dir: str | Path) -> AveragedPerceptronTagger:
        root = Path(model_dir)
        try:
            weights = _read_json(root / f"{_TAGGER_STEM}.weights.json")
            classes = _read_json(root / f"{_TAGGER_STEM}.classes.json")
            tagdict = _read_json(root / f"{_TAGGER_STEM}.tagdict.json")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FrontendDataError(f"Could not load Kokoro POS data: {exc}") from exc
        if not isinstance(weights, dict) or not isinstance(classes, list) or not isinstance(
            tagdict, dict
        ):
            raise FrontendDataError("Kokoro POS data has an invalid shape")
        return cls(weights=weights, classes=classes, tagdict=tagdict)

    @staticmethod
    def _normalize(word: str) -> str:
        if "-" in word and not word.startswith("-"):
            return "!HYPHEN"
        if word.isdigit() and len(word) == 4:
            return "!YEAR"
        if word and word[0].isdigit():
            return "!DIGITS"
        return word.casefold()

    @staticmethod
    def _features(
        index: int,
        word: str,
        context: Sequence[str],
        previous: str,
        previous2: str,
    ) -> tuple[str, ...]:
        i = index + 2
        return (
            "bias",
            f"i suffix {word[-3:]}",
            f"i pref1 {word[:1]}",
            f"i-1 tag {previous}",
            f"i-2 tag {previous2}",
            f"i tag+i-2 tag {previous} {previous2}",
            f"i word {context[i]}",
            f"i-1 tag+i word {previous} {context[i]}",
            f"i-1 word {context[i - 1]}",
            f"i-1 suffix {context[i - 1][-3:]}",
            f"i-2 word {context[i - 2]}",
            f"i+1 word {context[i + 1]}",
            f"i+1 suffix {context[i + 1][-3:]}",
            f"i+2 word {context[i + 2]}",
        )

    def _predict(self, features: Sequence[str]) -> str:
        scores: dict[str, float] = defaultdict(float)
        for feature in features:
            for label, weight in self._weights.get(feature, {}).items():
                scores[label] += weight
        return max(self._classes, key=lambda label: (scores[label], label))

    def tag(self, words: Sequence[str]) -> list[str]:
        context = (
            *self._START,
            *(self._normalize(word) for word in words),
            *self._END,
        )
        previous, previous2 = self._START
        result: list[str] = []
        for index, word in enumerate(words):
            tag = self._tagdict.get(word) or self._tagdict.get(word.casefold())
            if tag is None:
                tag = self._predict(
                    self._features(index, word, context, previous, previous2)
                )
            result.append(tag)
            previous2, previous = previous, tag
        return result


class OnnxG2PAdapter:
    """Greedy BART G2P decoding directly through ONNX Runtime.

    ONNX Runtime and NumPy are imported only when the default session is built,
    preserving the speech package's lazy import contract.
    """

    def __init__(
        self,
        model_path: str | Path,
        config_path: str | Path,
        *,
        session: Any | None = None,
    ) -> None:
        try:
            config = _read_json(Path(config_path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FrontendDataError(f"Could not load Kokoro G2P config: {exc}") from exc
        if not isinstance(config, dict):
            raise FrontendDataError("Kokoro G2P config has an invalid shape")
        try:
            self._bos = int(config["bos_token_id"])
            self._decoder_start = int(config["decoder_start_token_id"])
            self._eos = int(config["eos_token_id"])
            self._max_length = int(config["max_length"])
            grapheme_chars = str(config["grapheme_chars"])
            phoneme_chars = str(config["phoneme_chars"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FrontendDataError("Kokoro G2P config is incomplete") from exc
        self._grapheme_to_token = {
            grapheme: index for index, grapheme in enumerate(grapheme_chars)
        }
        self._token_to_phoneme = {
            index: phoneme for index, phoneme in enumerate(phoneme_chars)
        }
        self._session = session or self._create_session(Path(model_path))
        input_names = [input_.name for input_ in self._session.get_inputs()]
        if input_names != ["input_ids", "decoder_input_ids"]:
            raise FrontendDataError(
                f"Unexpected Kokoro G2P inputs: {', '.join(input_names)}"
            )

    @staticmethod
    def _create_session(model_path: Path) -> Any:
        if not model_path.is_file():
            raise FrontendDataError(f"Kokoro G2P model is missing: {model_path.name}")
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        return ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def phonemize(self, word: str) -> str:
        # TRANSCRIPT DATA -- DO NOT LOG.
        import numpy as np

        source = [
            self._bos,
            *(self._grapheme_to_token.get(char, 3) for char in word),
            self._eos,
        ]
        decoder = [self._decoder_start]
        terminated = False
        for _ in range(max(1, self._max_length - 1)):
            outputs = self._session.run(
                None,
                {
                    "input_ids": np.asarray([source], dtype=np.int64),
                    "decoder_input_ids": np.asarray([decoder], dtype=np.int64),
                },
            )
            if not outputs:
                raise FrontendDataError("Kokoro G2P returned no logits")
            logits = np.asarray(outputs[0])
            if logits.ndim != 3 or logits.shape[0] != 1 or logits.shape[1] != len(
                decoder
            ):
                raise FrontendDataError("Kokoro G2P returned invalid logits")
            if not np.isfinite(logits[0, -1]).all():
                raise FrontendDataError("Kokoro G2P returned non-finite logits")
            token = int(np.argmax(logits[0, -1]))
            if token == self._eos:
                terminated = True
                break
            decoder.append(token)
        if not terminated:
            raise FrontendDataError("Kokoro G2P exceeded its output limit")
        phonemes = "".join(
            self._token_to_phoneme.get(token, "") for token in decoder if token > 3
        )
        if not phonemes:
            raise FrontendDataError("Kokoro G2P returned an empty pronunciation")
        return phonemes


class _EnglishLexicon:
    """Misaki-compatible US lexicon lookup with narrow morphology support."""

    def __init__(
        self,
        gold: Mapping[str, str | Mapping[str, str | None]],
        silver: Mapping[str, str],
    ) -> None:
        self._gold = dict(gold)
        self._silver = dict(silver)

    @classmethod
    def from_model_dir(cls, model_dir: Path) -> _EnglishLexicon:
        try:
            gold = _read_json(model_dir / "us_gold.json")
            silver = _read_json(model_dir / "us_silver.json")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FrontendDataError(f"Could not load Kokoro lexicon data: {exc}") from exc
        if not isinstance(gold, dict) or not isinstance(silver, dict):
            raise FrontendDataError("Kokoro lexicon data has an invalid shape")
        for entry in gold.values():
            if not isinstance(entry, (str, dict)):
                raise FrontendDataError("Kokoro gold lexicon has an invalid entry")
        if not all(isinstance(entry, str) for entry in silver.values()):
            raise FrontendDataError("Kokoro silver lexicon has an invalid entry")
        return cls(gold, silver)

    @staticmethod
    def _parent_tag(tag: str) -> str:
        if tag.startswith("VB"):
            return "VERB"
        if tag.startswith("NN"):
            return "NOUN"
        if tag.startswith(("RB", "ADV")):
            return "ADV"
        if tag.startswith(("JJ", "ADJ")):
            return "ADJ"
        return tag

    @staticmethod
    def _apply_stress(phonemes: str, stress: float | None) -> str:
        if stress is None:
            return phonemes
        if stress < -1:
            return phonemes.replace("ˈ", "").replace("ˌ", "")
        if stress <= 0:
            return phonemes.replace("ˈ", "ˌ")
        if stress >= 1 and "ˈ" not in phonemes:
            return phonemes.replace("ˌ", "ˈ", 1)
        return phonemes

    def _raw_lookup(self, word: str, tag: str) -> str | None:
        entry = self._gold.get(word)
        if entry is None:
            entry = self._gold.get(word.casefold())
        if entry is None:
            entry = self._silver.get(word)
        if entry is None:
            entry = self._silver.get(word.casefold())
        if isinstance(entry, dict):
            entry = entry.get(tag) or entry.get(self._parent_tag(tag)) or entry.get(
                "DEFAULT"
            )
        return entry if isinstance(entry, str) else None

    def _spell_initialism(self, word: str) -> str | None:
        if len(word) < 2 or word != word.upper() or not word.isalpha():
            return None
        parts = [self._raw_lookup(char, "NNP") for char in word]
        if any(part is None for part in parts):
            return None
        spoken = [part or "" for part in parts]
        for index in range(len(spoken) - 1):
            spoken[index] = spoken[index].replace("ˈ", "ˌ")
        return "".join(spoken)

    @staticmethod
    def _pluralize(stem: str) -> str:
        if not stem:
            return stem
        if stem[-1] in "ptkfθ":
            return stem + "s"
        if stem[-1] in "szʃʒʧʤ":
            return stem + "ᵻz"
        return stem + "z"

    @staticmethod
    def _past(stem: str) -> str:
        if not stem:
            return stem
        if stem[-1] in "pkfθʃsʧ":
            return stem + "t"
        if stem[-1] == "d":
            return stem + "ᵻd"
        if stem[-1] != "t":
            return stem + "d"
        return stem + "ᵻd"

    @staticmethod
    def _progressive(stem: str) -> str:
        if not stem:
            return stem
        if len(stem) > 1 and stem[-1] == "t" and stem[-2] in "AIOWYiuæɑəɛɪʊʌ":
            return stem[:-1] + "ɾɪŋ"
        return stem + "ɪŋ"

    def _morphology(self, word: str, tag: str) -> str | None:
        lower = word.casefold()
        candidates: list[tuple[str, str]] = []
        if lower.endswith("ies") and len(lower) > 4:
            candidates.append((lower[:-3] + "y", "s"))
        if lower.endswith("es") and len(lower) > 3:
            candidates.append((lower[:-2], "s"))
        if lower.endswith("s") and not lower.endswith("ss"):
            candidates.append((lower[:-1], "s"))
        if lower.endswith("ed") and len(lower) > 3:
            candidates.extend(((lower[:-2], "ed"), (lower[:-1], "ed")))
        if lower.endswith("ing") and len(lower) > 4:
            candidates.extend(
                (
                    (lower[:-3], "ing"),
                    (lower[:-3] + "e", "ing"),
                    (lower[:-4], "ing"),
                )
            )
        for stem_word, suffix in candidates:
            stem = self._raw_lookup(stem_word, tag)
            if stem is None:
                continue
            if suffix == "s":
                return self._pluralize(stem)
            if suffix == "ed":
                return self._past(stem)
            return self._progressive(stem)
        return None

    def pronounce(self, word: str, tag: str, context: _TokenContext) -> str | None:
        normalized = unicodedata.normalize("NFKC", word).replace("’", "'")
        lower = normalized.casefold()
        if lower == "a" and tag == "DT":
            return "ɐ"
        if lower == "an" and tag == "DT":
            return "ɐn"
        if normalized == "I" and tag == "PRP":
            return "ˌI"
        if lower == "the":
            return "ði" if context.future_vowel is True else "ðə"
        if lower == "to":
            if context.future_vowel is None:
                return self._raw_lookup(normalized, tag)
            return "tʊ" if context.future_vowel else "tə"
        if lower == "used" and tag in {"VBD", "JJ"} and context.future_to:
            entry = self._gold.get("used")
            if isinstance(entry, dict):
                value = entry.get("VBD")
                if isinstance(value, str):
                    return value
        direct = self._raw_lookup(normalized, tag)
        if direct is not None:
            return direct
        spelled = self._spell_initialism(normalized)
        if spelled is not None:
            return spelled
        return self._morphology(normalized, tag)


@dataclass(frozen=True, eq=False)
class KokoroFrontendData:
    """Immutable language data a frontend needs, without any ONNX session.

    The lexicon and tagger dictionaries are large; rebuilding them repeatedly
    fragments CPython arenas. Holding this object lets an engine release and
    rebuild the small G2P session per load while keeping the data resident.
    """

    lexicon: _EnglishLexicon
    tagger: AveragedPerceptronTagger
    vocabulary: frozenset[str]

    @classmethod
    def from_model_dir(
        cls,
        model_dir: str | Path,
        *,
        tagger: AveragedPerceptronTagger | None = None,
    ) -> KokoroFrontendData:
        root = Path(model_dir)
        lexicon = _EnglishLexicon.from_model_dir(root)
        tagger = tagger or AveragedPerceptronTagger.from_model_dir(root)
        try:
            config = _read_json(root / "config.json")
            vocab = config["vocab"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FrontendDataError(f"Could not load Kokoro vocabulary: {exc}") from exc
        if not isinstance(vocab, dict) or not vocab:
            raise FrontendDataError("Kokoro vocabulary has an invalid shape")
        return cls(lexicon, tagger, frozenset(str(char) for char in vocab))


class KokoroEnglishFrontend:
    """Convert English text to validated Kokoro phonemes."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        fallback: G2PAdapter | None = None,
        tagger: AveragedPerceptronTagger | None = None,
        overrides: Mapping[str, str] | None = None,
        data: KokoroFrontendData | None = None,
    ) -> None:
        self._model_dir = Path(model_dir)
        if data is None:
            data = KokoroFrontendData.from_model_dir(self._model_dir, tagger=tagger)
        self.data = data
        self._lexicon = data.lexicon
        self._tagger = data.tagger
        self._vocabulary = data.vocabulary
        self._fallback = fallback or OnnxG2PAdapter(
            self._model_dir / "g2p-en-us.onnx",
            self._model_dir / "g2p-en-us.json",
        )
        self._overrides = {
            word.casefold(): self._validate_phonemes(phonemes)
            for word, phonemes in (overrides or {}).items()
        }

    @staticmethod
    def _tokenize(text: str) -> list[_Token]:
        tokens: list[_Token] = []
        matches = list(_TOKEN_RE.finditer(text.lstrip()))
        sentence_start = True
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(
                text.lstrip()
            )
            whitespace = text.lstrip()[match.end() : end]
            token_text = match.group()
            phonemes: str | None = None
            override = _INLINE_OVERRIDE.fullmatch(token_text)
            if override:
                token_text = override.group("label")
                phonemes = override.group("phonemes")
            token = _Token(
                text=token_text,
                whitespace=whitespace,
                phonemes=phonemes,
                sentence_start=sentence_start,
            )
            tokens.append(token)
            if token_text in ".!?":
                sentence_start = True
            elif token_text not in _PUNCTUATION:
                sentence_start = False
        return tokens

    @staticmethod
    def _is_numeric(text: str) -> bool:
        return any(char.isdigit() for char in text) and all(
            char.isdigit() or char in ".,:_-+'abcdefghijklmnopqrstuvwxyz"
            for char in text.casefold()
        )

    def _tag(self, tokens: Sequence[_Token]) -> None:
        words = [token.text for token in tokens]
        tags = self._tagger.tag(words)
        for token, tag in zip(tokens, tags, strict=True):
            if token.text in _CURRENCY_SYMBOLS:
                token.tag = "$"
            elif self._is_numeric(token.text):
                token.tag = "CD"
            elif token.text in _PUNCTUATION or token.text in _SILENT_JUNKS:
                token.tag = token.text if token.text not in ".!?" else "."
            else:
                token.tag = tag
            if (
                token.sentence_start
                and token.text.casefold() in _IMPERATIVE_HOMOGRAPHS
            ):
                token.tag = "VB"

    def _validate_phonemes(self, phonemes: str) -> str:
        unknown = sorted(
            {
                char
                for char in phonemes
                if not char.isspace() and char not in self._vocabulary
            }
        )
        if unknown:
            rendered = " ".join(repr(char) for char in unknown)
            raise InvalidPhonemeError(
                f"Pronunciation contains characters not in the Kokoro vocabulary: {rendered}"
            )
        return phonemes

    def _number_context(
        self,
        tokens: Sequence[_Token],
        index: int,
        currency: str | None,
    ) -> NumberContext:
        text = tokens[index].text
        previous = tokens[index - 1].text.casefold() if index else ""
        return NumberContext(
            currency=currency,
            digit_by_digit=text.count("-") >= 2,
            time=":" in text,
            version=text.count(".") > 1 or previous in {"v", "ver", "version"},
        )

    def _pronounce_number(
        self,
        words: Sequence[str],
        context: _TokenContext,
    ) -> str:
        result: list[str] = []
        nested_context = context
        for word in reversed(words):
            phonemes = self._lexicon.pronounce(word, "CD", nested_context)
            if phonemes is None:
                phonemes = self._fallback.phonemize(word)
            if not phonemes:
                raise UnpronounceableTextError(
                    f"No pronunciation for number word {word!r}"
                )
            result.append(self._validate_phonemes(phonemes))
            nested_context = _context_from_phonemes(phonemes, word)
        return " ".join(reversed(result))

    def _pronounce_identifier(
        self,
        word: str,
        tag: str,
        context: _TokenContext,
    ) -> str | None:
        parts = _IDENTIFIER_PARTS.findall(word)
        if len(parts) < 2 or "".join(parts) != word:
            return None
        pronunciations: list[str] = []
        nested_context = context
        for part in reversed(parts):
            phonemes = self._lexicon.pronounce(part, tag, nested_context)
            if phonemes is None:
                return None
            pronunciations.append(phonemes)
            nested_context = _context_from_phonemes(phonemes, part)
        pronunciations.reverse()
        for index in range(len(pronunciations) - 1):
            pronunciations[index] = pronunciations[index].replace("ˈ", "ˌ")
        return "".join(pronunciations)

    def phonemize(self, text: str) -> str:
        """Return validated Kokoro phonemes for English input.

        TRANSCRIPT DATA -- DO NOT LOG.
        """

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text.strip():
            return ""
        tokens = self._tokenize(text)
        if not tokens:
            return ""
        self._tag(tokens)
        resolved: list[str] = [""] * len(tokens)
        context = _TokenContext()
        currency_for: dict[int, str] = {}
        for index, token in enumerate(tokens[:-1]):
            if token.text in _CURRENCY_SYMBOLS and self._is_numeric(
                tokens[index + 1].text
            ):
                currency_for[index + 1] = token.text

        for index in range(len(tokens) - 1, -1, -1):
            token = tokens[index]
            phonemes: str | None
            if token.text in _CURRENCY_SYMBOLS:
                phonemes = ""
            elif token.text in _SILENT_JUNKS:
                phonemes = ""
            elif token.text in _SYMBOL_WORDS:
                symbol_word = _SYMBOL_WORDS[token.text]
                phonemes = self._lexicon.pronounce(symbol_word, token.tag, context)
                if phonemes is None:
                    phonemes = self._fallback.phonemize(symbol_word)
                if not phonemes:
                    raise UnpronounceableTextError(
                        f"No pronunciation for symbol {token.text!r}"
                    )
                if index > 0 and not tokens[index - 1].whitespace:
                    phonemes = " " + phonemes
                if index + 1 < len(tokens) and not token.whitespace:
                    phonemes += " "
            elif token.text in _PUNCTUATION:
                phonemes = token.text if token.text in self._vocabulary else ""
            elif token.phonemes is not None:
                phonemes = self._validate_phonemes(token.phonemes)
            elif token.text.casefold() in self._overrides:
                phonemes = self._overrides[token.text.casefold()]
            elif self._is_numeric(token.text):
                try:
                    words = expand_number(
                        token.text,
                        context=self._number_context(
                            tokens, index, currency_for.get(index)
                        ),
                    )
                except ValueError as exc:
                    raise UnpronounceableTextError(
                        f"Could not normalize number {token.text!r}"
                    ) from exc
                phonemes = self._pronounce_number(words, context)
            else:
                phonemes = self._lexicon.pronounce(token.text, token.tag, context)
                if phonemes is None:
                    phonemes = self._pronounce_identifier(
                        token.text, token.tag, context
                    )
                if phonemes is None:
                    phonemes = self._fallback.phonemize(token.text)
                if not phonemes:
                    raise UnpronounceableTextError(
                        f"No pronunciation for token {token.text!r}"
                    )
                phonemes = self._validate_phonemes(phonemes)
            # Kokoro v1 uses an ASCII alias for the US flap and glottal stop.
            phonemes = phonemes.replace("ɾ", "T").replace("ʔ", "t")
            phonemes = self._validate_phonemes(phonemes)
            resolved[index] = phonemes
            if token.text in _NON_QUOTE_PUNCTUATION:
                context = _TokenContext()
            elif phonemes:
                context = _context_from_phonemes(
                    phonemes,
                    token.text,
                    future_to=token.text.casefold() == "to"
                    and token.tag in {"TO", "IN"},
                )
        return "".join(
            phonemes + token.whitespace
            for token, phonemes in zip(tokens, resolved, strict=True)
        ).strip()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _context_from_phonemes(
    phonemes: str,
    token_text: str,
    *,
    future_to: bool = False,
) -> _TokenContext:
    future_vowel: bool | None = None
    for char in phonemes:
        if char in _VOWELS:
            future_vowel = True
            break
        if char in _CONSONANTS:
            future_vowel = False
            break
        if char in _NON_QUOTE_PUNCTUATION:
            future_vowel = None
            break
    return _TokenContext(future_vowel=future_vowel, future_to=future_to)


__all__ = [
    "AveragedPerceptronTagger",
    "FrontendDataError",
    "G2PAdapter",
    "InvalidPhonemeError",
    "KokoroEnglishFrontend",
    "KokoroFrontendData",
    "OnnxG2PAdapter",
]
