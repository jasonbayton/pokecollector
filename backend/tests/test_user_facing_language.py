import pathlib
import re
import unittest


BACKEND = pathlib.Path(__file__).resolve().parent.parent

# Upstream is German-authored, and several of its HTTPException details are
# rendered raw to the user by the scan review inbox, so they surfaced as German
# sentences in an otherwise English interface. Translating them once is not
# enough: every merge forward from upstream can reintroduce them, and nothing
# pinned them before this test existed.
SEARCHED = ("api", "services")

# Accent folding data, not user-facing copy. It exists precisely to hold
# accented characters, so it can never be part of this check.
ALLOWED = {"text_search.py"}

GERMAN_CHARACTERS = re.compile(r"[äöüÄÖÜß]")

# A character mapping is not a message. Transliteration code has to name the
# characters it folds, so `.replace("ß", "ss")` in a normaliser is legitimate
# and must not be read as German prose. Deliberately narrow: it matches a
# single character being replaced, so a German sentence cannot hide behind it.
TRANSLITERATION = re.compile(r"""\.replace\(\s*["'].["']\s*,\s*["'][^"']{0,4}["']\s*\)""")

# Words that are German rather than shared with English, so a hit is decisive
# rather than a guess. Deliberately not "in" or "die", which are both.
GERMAN_WORDS = re.compile(
    r"\b(nicht|konnte|werden|wurde|Karte|Fehler|bitte|Bitte|meldet|verf[üu]gbar"
    r"|fehlgeschlagen|Ung[üu]ltig|[üu]berlastet|erreicht|pr[üu]fen|versuchen)\b"
)


def _candidate_files():
    for folder in SEARCHED:
        for path in sorted((BACKEND / folder).rglob("*.py")):
            if path.name in ALLOWED:
                continue
            yield path


class UserFacingLanguageTests(unittest.TestCase):
    """The API's own messages reach users unmodified, so they must be English."""

    def test_no_german_characters_in_backend_messages(self):
        offenders = []
        for path in _candidate_files():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if TRANSLITERATION.search(line):
                    continue
                if GERMAN_CHARACTERS.search(line):
                    offenders.append(f"{path.relative_to(BACKEND)}:{number}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "German characters in backend source. These strings reach the user "
            "unmodified through the scan review inbox:\n" + "\n".join(offenders),
        )

    def test_no_german_words_in_error_details(self):
        offenders = []
        for path in _candidate_files():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if "detail" not in line and "send_message" not in line:
                    continue
                if GERMAN_WORDS.search(line):
                    offenders.append(f"{path.relative_to(BACKEND)}:{number}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "German wording in a message that reaches the user:\n" + "\n".join(offenders),
        )

    def test_the_guard_would_actually_fire(self):
        # A guard that cannot fail is decoration. Both patterns are checked
        # against the exact strings this fork had to translate.
        self.assertTrue(GERMAN_CHARACTERS.search("Ungültiger Gemini API Key."))
        # The exemption must not swallow a real message that happens to contain
        # a replace() call elsewhere on the line.
        self.assertTrue(TRANSLITERATION.search('.replace("ß", "ss")'))
        self.assertFalse(TRANSLITERATION.search('detail="Ungültiger Key"'))
        self.assertTrue(GERMAN_WORDS.search("Kartenname konnte nicht erkannt werden."))
        self.assertFalse(GERMAN_CHARACTERS.search("The card name could not be read."))
        self.assertFalse(GERMAN_WORDS.search("The card name could not be read."))


if __name__ == "__main__":
    unittest.main()
