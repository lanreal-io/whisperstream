from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path

import numpy as np
import pytest

from whisperstream.speech.kokoro_frontend import (
    AveragedPerceptronTagger,
    FrontendDataError,
    InvalidPhonemeError,
    KokoroEnglishFrontend,
    OnnxG2PAdapter,
    UnpronounceableTextError,
)
from whisperstream.speech.number_normalizer import (
    NumberContext,
    expand_number,
)


FORBIDDEN_IMPORTS = {
    "espeak",
    "espeakng_loader",
    "kokoro_onnx",
    "misaki",
    "nltk",
    "num2words",
    "phonemizer",
    "spacy",
    "torch",
    "transformers",
}


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


@pytest.fixture
def frontend_model_dir(tmp_path: Path) -> Path:
    gold = {
        "a": "A",
        "agent": "ˈAʤᵊnt",
        "an": "æn",
        "and": "ænd",
        "cable": "kˈAbᵊl",
        "cent": "sˈɛnt",
        "dollar": "dˈɑləɹ",
        "eight": "ˈAt",
        "fifty": "fˈɪfti",
        "five": "fˈIv",
        "four": "fˈɔɹ",
        "hundred": "hˈʌndɹəd",
        "I": "ˈI",
        "in": "ɪn",
        "issue": "ˈɪʃu",
        "it": "ɪt",
        "nine": "nˈIn",
        "now": "nˈW",
        "object": {"DEFAULT": "ˈɑbʤəkt", "VERB": "əbʤˈɛkt"},
        "one": "wˈʌn",
        "open": "ˈOpᵊn",
        "percent": "pəɹsˈɛnt",
        "point": "pˈYnt",
        "read": {
            "DEFAULT": "ɹˈid",
            "VBD": "ɹˈɛd",
            "VBN": "ɹˈɛd",
            "VBP": "ɹˈɛd",
        },
        "ready": "ɹˈɛdi",
        "record": {"DEFAULT": "ɹˈɛkəɹd", "VERB": "ɹəkˈɔɹd"},
        "refuse": {"DEFAULT": "ɹəfjˈuz", "NOUN": "ɹˈɛfjˌus"},
        "response": "ɹəspˈɑns",
        "review": "ɹəvjˈu",
        "seven": "sˈɛvən",
        "six": "sˈɪks",
        "test": "tˈɛst",
        "the": "ði",
        "three": "θɹˈi",
        "thirty": "θˈɜɹɾi",
        "to": "tu",
        "twenty": "twˈɛnti",
        "two": "tˈu",
        "used": {"DEFAULT": "jˈuzd", "VBD": "jˈust"},
        "will": "wɪl",
        "wind": {"DEFAULT": "wˈɪnd", "VERB": "wˈInd"},
        "zero": "zˈɪɹO",
    }
    silver = {
        "cents": "sˈɛnts",
        "dollars": "dˈɑləɹz",
        "opened": "ˈOpᵊnd",
        "tests": "tˈɛsts",
    }
    _write_json(tmp_path / "us_gold.json", gold)
    _write_json(tmp_path / "us_silver.json", silver)

    classes = ["DT", "MD", "NN", "PRP", "RB", "TO", "VB", "VBD"]
    tagdict = {
        "a": "DT",
        "an": "DT",
        "I": "PRP",
        "it": "PRP",
        "now": "RB",
        "the": "DT",
        "to": "TO",
        "will": "MD",
        "yesterday": "RB",
    }
    weights = {
        "bias": {"NN": 1.0},
        "i word opened": {"VBD": 5.0},
        "i word read": {"VBD": 3.0},
        "i word used": {"VBD": 5.0},
        "i-1 tag MD": {"VB": 8.0},
        "i-1 tag TO": {"VB": 8.0},
    }
    stem = "averaged_perceptron_tagger_eng"
    _write_json(tmp_path / f"{stem}.classes.json", classes)
    _write_json(tmp_path / f"{stem}.tagdict.json", tagdict)
    _write_json(tmp_path / f"{stem}.weights.json", weights)

    phonemes = set(" ;:,.!?\"()")
    for value in [*gold.values(), *silver.values()]:
        if isinstance(value, str):
            phonemes.update(value)
        else:
            for pronunciation in value.values():
                if pronunciation:
                    phonemes.update(pronunciation)
    phonemes.update("kəɐoɹvɜʒnætʧɛksɔɪzʌmpTɹuɑθwˌb")
    _write_json(
        tmp_path / "config.json",
        {"vocab": {char: index + 1 for index, char in enumerate(sorted(phonemes))}},
    )
    return tmp_path


class _Fallback:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.mapping = {
            "Version": "vˈɜɹʒən",
            "at": "æt",
            "checked": "ʧˈɛkt",
            "costs": "kˈɔsts",
            "is": "ɪz",
            "Kokoro": "kəkˈoɹO",
            "NumPy": "nˈʌmpi",
            "PyTorch": "pˈITəɹk",
            "Ruatha": "ɹuˈɑθə",
            "WebSocket": "wˌɛbsˈɑkət",
            "yesterday": "jˈɛstəɹdˌA",
        }

    def phonemize(self, word: str) -> str:
        self.calls.append(word)
        return self.mapping.get(word, "")


def test_frontend_modules_do_not_import_forbidden_packages() -> None:
    root = Path(__file__).parents[1] / "src" / "whisperstream" / "speech"
    imported: set[str] = set()
    for filename in ("kokoro_frontend.py", "number_normalizer.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
    assert imported.isdisjoint(FORBIDDEN_IMPORTS)


@pytest.mark.parametrize(
    ("token", "context", "expected"),
    [
        ("0", NumberContext(), ("zero",)),
        ("19", NumberContext(), ("nineteen",)),
        ("105", NumberContext(), ("one", "hundred", "five")),
        ("2026", NumberContext(), ("twenty", "twenty", "six")),
        ("21st", NumberContext(), ("twenty", "first")),
        ("1.3.0", NumberContext(version=True), ("one", "three", "zero")),
        ("3:30", NumberContext(time=True), ("three", "thirty")),
        (
            "29.50",
            NumberContext(currency="$"),
            ("twenty", "nine", "dollars", "and", "fifty", "cents"),
        ),
        (
            "800-555-0100",
            NumberContext(digit_by_digit=True),
            ("eight", "zero", "zero", "five", "five", "five", "zero", "one", "zero", "zero"),
        ),
    ],
)
def test_original_number_normalizer(
    token: str,
    context: NumberContext,
    expected: tuple[str, ...],
) -> None:
    assert expand_number(token, context=context) == expected


def test_minimal_perceptron_tagger_uses_context(frontend_model_dir: Path) -> None:
    tagger = AveragedPerceptronTagger.from_model_dir(frontend_model_dir)

    assert tagger.tag(["I", "read", "yesterday"]) == ["PRP", "VBD", "RB"]
    assert tagger.tag(["I", "will", "read", "it"]) == ["PRP", "MD", "VB", "PRP"]


def test_homographs_weak_forms_and_morphology(frontend_model_dir: Path) -> None:
    fallback = _Fallback()
    frontend = KokoroEnglishFrontend(frontend_model_dir, fallback=fallback)

    assert frontend.phonemize("Record the record.") == "ɹəkˈɔɹd ðə ɹˈɛkəɹd."
    assert (
        frontend.phonemize("I read the response yesterday. I will read it now.")
        == "ˌI ɹˈɛd ðə ɹəspˈɑns jˈɛstəɹdˌA. ˌI wɪl ɹˈid ɪt nˈW."
    )
    assert frontend.phonemize("The agent opened an issue.") == (
        "ði ˈAʤᵊnt ˈOpᵊnd ɐn ˈɪʃu."
    )
    assert frontend.phonemize("tests") == "tˈɛsts"


def test_locked_reference_parity_corpus(frontend_model_dir: Path) -> None:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "kokoro_frontend" / "parity.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["source"]["commit"] == "fba1236595f2d2bf21d414ba6e57d25256afada3"
    frontend = KokoroEnglishFrontend(frontend_model_dir, fallback=_Fallback())

    for case in fixture["cases"]:
        assert frontend.phonemize(case["text"]) == case["expected"], case["id"]


def test_imperative_homograph_improvements(frontend_model_dir: Path) -> None:
    frontend = KokoroEnglishFrontend(frontend_model_dir, fallback=_Fallback())

    assert frontend.phonemize("Object to the object.") == (
        "əbʤˈɛkt tə ði ˈɑbʤəkt."
    )
    assert frontend.phonemize("Wind the cable.") == "wˈInd ðə kˈAbᵊl."


def test_numbers_agent_terms_and_punctuation(frontend_model_dir: Path) -> None:
    fallback = _Fallback()
    frontend = KokoroEnglishFrontend(frontend_model_dir, fallback=fallback)

    assert frontend.phonemize("Version 1.3.0 costs $29.50 at 3:30.") == (
        "vˈɜɹʒən wˈʌn θɹˈi zˈɪɹO kˈɔsts "
        "twˈɛnti nˈIn dˈɑləɹz ænd fˈɪfti sˈɛnts æt "
        "θɹˈi θˈɜɹTi."
    )
    assert frontend.phonemize("Ruatha checked WebSocket and NumPy.") == (
        "ɹuˈɑθə ʧˈɛkt wˌɛbsˈɑkət ænd nˈʌmpi."
    )
    assert frontend.phonemize("50%") == "fˈɪfti pəɹsˈɛnt"
    assert Counter(fallback.calls) == Counter(
        ["Version", "costs", "at", "Ruatha", "checked", "WebSocket", "NumPy"]
    )


def test_pronunciation_overrides_are_validated(frontend_model_dir: Path) -> None:
    frontend = KokoroEnglishFrontend(
        frontend_model_dir,
        fallback=_Fallback(),
        overrides={"Kokoro": "kəkˈoɹO"},
    )

    assert frontend.phonemize("Kokoro is ready.") == "kəkˈoɹO ɪz ɹˈɛdi."
    assert frontend.phonemize("[Kokoro](/kəkˈoɹO/) is ready.") == (
        "kəkˈoɹO ɪz ɹˈɛdi."
    )

    with pytest.raises(InvalidPhonemeError, match="not in the Kokoro vocabulary"):
        frontend.phonemize("[Kokoro](/☃/)")


def test_unknown_word_fails_closed(frontend_model_dir: Path) -> None:
    frontend = KokoroEnglishFrontend(frontend_model_dir, fallback=_Fallback())

    with pytest.raises(UnpronounceableTextError, match="No pronunciation"):
        frontend.phonemize("Unmapped")


class _FakeInput:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeG2PSession:
    def __init__(self) -> None:
        self.inputs = [_FakeInput("input_ids"), _FakeInput("decoder_input_ids")]
        self.decoder_tokens = [4, 5, 2]
        self.calls: list[dict[str, np.ndarray]] = []

    def get_inputs(self) -> list[_FakeInput]:
        return self.inputs

    def run(
        self,
        _output_names: object,
        feeds: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        self.calls.append(feeds)
        token = self.decoder_tokens[len(feeds["decoder_input_ids"][0]) - 1]
        logits = np.full((1, len(feeds["decoder_input_ids"][0]), 6), -10, dtype=np.float32)
        logits[0, -1, token] = 10
        return [logits]


def test_direct_onnx_g2p_adapter_has_no_transformers_dependency(tmp_path: Path) -> None:
    config_path = tmp_path / "g2p-en-us.json"
    _write_json(
        config_path,
        {
            "bos_token_id": 1,
            "decoder_start_token_id": 1,
            "eos_token_id": 2,
            "grapheme_chars": "____ab",
            "max_length": 8,
            "pad_token_id": 0,
            "phoneme_chars": "____ɹu",
        },
    )
    session = _FakeG2PSession()
    adapter = OnnxG2PAdapter(
        tmp_path / "g2p-en-us.onnx",
        config_path,
        session=session,
    )

    assert adapter.phonemize("ab") == "ɹu"
    assert session.calls[0]["input_ids"].dtype == np.int64
    assert session.calls[-1]["decoder_input_ids"].tolist() == [[1, 4, 5]]


def test_shared_frontend_data_gives_identical_output_with_fresh_g2p(
    frontend_model_dir: Path,
) -> None:
    from whisperstream.speech.kokoro_frontend import KokoroFrontendData

    fixture_path = (
        Path(__file__).parent / "fixtures" / "kokoro_frontend" / "parity.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    data = KokoroFrontendData.from_model_dir(frontend_model_dir)
    first_fallback, second_fallback = _Fallback(), _Fallback()
    first = KokoroEnglishFrontend(frontend_model_dir, fallback=first_fallback, data=data)
    second = KokoroEnglishFrontend(frontend_model_dir, fallback=second_fallback, data=data)

    assert first.data is second.data is data
    assert first._fallback is not second._fallback  # noqa: SLF001
    for case in fixture["cases"]:
        assert first.phonemize(case["text"]) == case["expected"], case["id"]
        assert second.phonemize(case["text"]) == case["expected"], case["id"]
