import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
