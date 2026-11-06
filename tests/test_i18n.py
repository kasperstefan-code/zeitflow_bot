import string

import pytest

from zeitflow.i18n import COMMAND_DESCRIPTIONS, LANGUAGES, TEXTS, WORKER_COMMANDS, t


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@pytest.mark.parametrize("lang", list(LANGUAGES))
def test_every_language_is_complete(lang):
    assert set(TEXTS[lang]) == set(TEXTS["de"])
    for key, text in TEXTS["de"].items():
        assert placeholders(TEXTS[lang][key]) == placeholders(text), f"{lang}.{key}"


@pytest.mark.parametrize("lang", list(LANGUAGES))
def test_command_descriptions(lang):
    assert len(COMMAND_DESCRIPTIONS[lang]) == len(WORKER_COMMANDS)
    assert all(3 <= len(d) <= 256 for d in COMMAND_DESCRIPTIONS[lang])


def test_menu_mentions_all_worker_commands():
    for lang in LANGUAGES:
        for cmd in WORKER_COMMANDS:
            assert f"/{cmd}" in TEXTS[lang]["menu"]


def test_unknown_language_falls_back_to_german():
    assert t("xx", "cancelled") == "Abgebrochen."
    assert t("ru", "saved", count=3) == "✅ Сохранено (записей: 3)."
