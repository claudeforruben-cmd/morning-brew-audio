import email.message
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brew  # noqa: E402


def episode(day: int) -> brew.Episode:
    return brew.Episode(
        id=f"id{day}", date=f"2026-09-{day:02d}", title=f"Sep {day} & friends",
        file=f"episodes/2026-09-{day:02d}.mp3", bytes=1234, summary="Sum <b>",
        published="Sat, 19 Sep 2026 05:20:05 -0400",
    )


class ChunkText(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(brew.chunk_text("Hello.\n\nWorld."), ["Hello.\n\nWorld."])

    def test_chunks_respect_limit_and_lose_nothing(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 200 for i in range(20))
        chunks = brew.chunk_text(text, limit=1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual(" ".join(" ".join(chunks).split()), " ".join(text.split()))

    def test_oversized_single_sentence_is_hard_split(self):
        chunks = brew.chunk_text("word " * 500, limit=300)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertEqual(sum(c.count("word") for c in chunks), 500)


class Dates(unittest.TestCase):
    def test_ordinals(self):
        got = [brew.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 30)]
        self.assertEqual(got, ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th",
                               "21st", "22nd", "23rd", "30th"])

    def test_spoken_date(self):
        self.assertEqual(brew.spoken_date(datetime(2026, 9, 19)), "Saturday, September 19th")


class Fidelity(unittest.TestCase):
    def test_flags_only_numbers_missing_from_source(self):
        source = "Stock fell 4.67% to $71.79 and Buffett is 96. Revenue: 5,500,000%."
        script = "Good morning. 19th.\n\nStock fell 4.67 percent to $71.79. Buffett is 96. Revenue 5,500,000 percent. Sales hit 42."
        self.assertEqual(brew.unsupported_numbers(script, source), ["42"])


class GeminiFallback(unittest.TestCase):
    def http_error(self, code):
        import io
        import urllib.error
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b'{"error":"boom"}'))

    def test_skips_missing_and_out_of_quota_models_then_succeeds(self):
        calls = []

        def fake(model, system, user):
            calls.append(model)
            if len(calls) == 1:
                raise self.http_error(404)
            if len(calls) == 2:
                raise self.http_error(429)
            return "the script"

        with mock.patch.object(brew, "_gemini_call", fake), \
                mock.patch.object(brew, "SCRIPT_MODEL", ""):
            self.assertEqual(brew._llm_gemini("s", "u"), "the script")
        self.assertEqual(calls, brew.GEMINI_MODELS[:3])

    def test_bad_key_fails_immediately_without_trying_other_models(self):
        import urllib.error
        calls = []

        def fake(model, system, user):
            calls.append(model)
            raise self.http_error(403)

        with mock.patch.object(brew, "_gemini_call", fake), \
                mock.patch.object(brew, "SCRIPT_MODEL", ""):
            with self.assertRaises(urllib.error.HTTPError):
                brew._llm_gemini("s", "u")
        self.assertEqual(len(calls), 1)

    def test_all_models_failing_raises_readable_error(self):
        with mock.patch.object(brew, "_gemini_call",
                               lambda *a: (_ for _ in ()).throw(self.http_error(404))), \
                mock.patch.object(brew, "SCRIPT_MODEL", ""):
            with self.assertRaisesRegex(RuntimeError, "No Gemini model produced a script"):
                brew._llm_gemini("s", "u")


class HtmlToText(unittest.TestCase):
    def test_strips_markup_and_images(self):
        out = brew.html_to_text(
            "<html><head><title>x</title></head><body><p>Hi <b>there</b></p>"
            "<img src='a.gif'><script>bad()</script><p>Bye</p></body></html>")
        self.assertIn("Hi", out)
        self.assertIn("Bye", out)
        self.assertNotIn("bad()", out)
        self.assertNotIn("a.gif", out)


class Feed(unittest.TestCase):
    def test_feed_is_valid_xml_with_escaped_content(self):
        xml = brew.build_feed([episode(19)], "https://example.com/", "tok")
        root = ET.fromstring(xml)
        item = root.find("channel/item")
        self.assertEqual(item.find("title").text, "Sep 19 & friends")
        enc = item.find("enclosure")
        self.assertEqual(enc.get("url"), "https://example.com/tok/episodes/2026-09-19.mp3")
        self.assertEqual(enc.get("length"), "1234")

    def test_stray_whitespace_in_url_and_token_is_stripped(self):
        xml = brew.build_feed([episode(19)], "  https://example.com/ ", " tok\n")
        root = ET.fromstring(xml)
        self.assertEqual(root.find("channel/link").text, "https://example.com/tok/feed.xml")
        self.assertEqual(root.find("channel/item/enclosure").get("url"),
                         "https://example.com/tok/episodes/2026-09-19.mp3")

    def test_publish_prunes_old_episodes(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            episodes: list[brew.Episode] = []
            for day in range(1, brew.KEEP_EPISODES + 3):
                ep = episode(day)
                brew.publish(storage, "tok", "https://x", ep, b"mp3", episodes)
                episodes = brew.load_episodes(storage, "tok")
            self.assertEqual(len(episodes), brew.KEEP_EPISODES)
            self.assertEqual(episodes[0].date, f"2026-09-{brew.KEEP_EPISODES + 2:02d}")
            self.assertFalse((Path(d) / "tok/episodes/2026-09-01.mp3").exists())
            self.assertTrue((Path(d) / "tok/feed.xml").exists())


    def test_untagged_episodes_from_the_old_feed_are_labeled_morning_brew(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            brew.publish(storage, "tok", "https://x", episode(19), b"mp3", [])
            titles = [e.title for e in brew.load_episodes(storage, "tok")]
            self.assertEqual(titles, ["Morning Brew · Sep 19 & friends"])


class Sources(unittest.TestCase):
    def test_each_source_has_its_own_label(self):
        labels = [s.label for s in brew.SOURCES.values()]
        self.assertEqual(len(set(labels)), len(labels))

    def test_prompts_format_without_stray_braces(self):
        for src in brew.SOURCES.values():
            out = src.prompt.format(spoken_date="Sunday, September 20th",
                                    publication=src.publication)
            self.assertIn("Sunday, September 20th", out)
            self.assertNotIn("{", out)


class FakeBox:
    """Stands in for Mailbox: `mails` is a list of (subject, iso date, message-id)."""

    def __init__(self, mails, body="Some newsletter text. " * 20):
        self.mails = mails
        self.body = body

    def search(self, senders, days=2):
        return [str(i).encode() for i in range(len(self.mails))]

    def headers(self, num):
        subject, date, mid = self.mails[int(num)]
        msg = email.message.EmailMessage()
        msg["Subject"] = subject
        msg["Date"] = date
        msg["Message-ID"] = mid
        return msg

    def message(self, num):
        msg = self.headers(num)
        msg.set_content(self.body)
        return msg


TZ = ZoneInfo("America/New_York")
FRI = ("Fri: Leaving LA", "Fri, 18 Sep 2026 09:20:09 +0000", "<fri@x>")
SAT = ("Sat: Buffett goodbye", "Sat, 19 Sep 2026 09:20:05 +0000", "<sat@x>")
WELCOME = ("Welcome to Markets P.M.", "Sun, 20 Sep 2026 12:42:58 +0000", "<w@x>")


class RunSource(unittest.TestCase):
    def run_it(self, src_key, mails, storage, force=False):
        with mock.patch.object(brew, "write_script", lambda *a: "Hi.\n\nScript body."), \
                mock.patch.object(brew, "synthesize", lambda script: b"mp3"):
            return brew.run_source(brew.SOURCES[src_key], FakeBox(mails), storage,
                                   "tok", "https://x", TZ, force)

    def test_new_feed_starts_with_only_the_latest_email(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            self.assertEqual(self.run_it("brew", [FRI, SAT], storage), 1)
            titles = [e.title for e in brew.load_episodes(storage, "tok")]
            self.assertEqual(titles, ["Morning Brew · Sep 19: Sat: Buffett goodbye"])

    def test_late_email_is_caught_up_even_after_a_newer_one_published(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            self.run_it("brew", [SAT], storage)
            self.assertEqual(self.run_it("brew", [FRI, SAT], storage), 1)
            self.assertEqual(len(brew.load_episodes(storage, "tok")), 2)

    def test_rerun_with_nothing_new_publishes_nothing_but_keeps_feed(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            self.run_it("brew", [SAT], storage)
            self.assertEqual(self.run_it("brew", [SAT], storage), 0)
            self.assertTrue((Path(d) / "tok/feed.xml").exists())

    def test_same_email_arriving_with_a_new_message_id_is_not_republished(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            self.run_it("brew", [SAT], storage)
            direct_copy = (SAT[0], SAT[1], "<different-id@x>")
            self.assertEqual(self.run_it("brew", [direct_copy], storage), 0)

    def test_welcome_email_never_becomes_an_episode(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            self.assertEqual(self.run_it("wsj", [WELCOME], storage), 0)
            self.assertEqual(brew.load_episodes(storage, "tok"), [])

    def test_all_sources_share_one_feed_with_labeled_titles(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            self.run_it("brew", [SAT], storage)
            self.run_it("wsj", [WELCOME, ("Markets P.M.: Stocks slide",
                                          "Mon, 21 Sep 2026 21:00:00 +0000", "<m@x>")], storage)
            titles = [e.title for e in brew.load_episodes(storage, "tok")]
            self.assertEqual(titles, ["WSJ · Sep 21: Markets P.M.: Stocks slide",
                                      "Morning Brew · Sep 19: Sat: Buffett goodbye"])
            feed = ET.fromstring((Path(d) / "tok/feed.xml").read_text())
            self.assertEqual(len(feed.findall("channel/item")), 2)
            self.assertFalse((Path(d) / "tok/wsj").exists())

    def test_two_editions_on_the_same_day_get_distinct_files(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            am = ("Markets A.M.: Futures", "Mon, 21 Sep 2026 12:00:00 +0000", "<am@x>")
            pm = ("Markets P.M.: Close", "Mon, 21 Sep 2026 21:00:00 +0000", "<pm@x>")
            self.run_it("wsj", [am], storage)
            self.run_it("wsj", [am, pm], storage)
            files = {e.file for e in brew.load_episodes(storage, "tok")}
            self.assertEqual(len(files), 2)

    def test_force_regenerates_the_newest_email(self):
        with tempfile.TemporaryDirectory() as d:
            storage = brew.LocalStorage(Path(d))
            self.run_it("brew", [SAT], storage)
            self.assertEqual(self.run_it("brew", [SAT], storage, force=True), 1)
            self.assertEqual(len(brew.load_episodes(storage, "tok")), 1)


if __name__ == "__main__":
    unittest.main()
