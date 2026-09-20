#!/usr/bin/env python3
"""Turn today's Morning Brew email into a podcast episode.

Pipeline: Gmail (IMAP) -> plain text -> spoken script (LLM) -> MP3 (TTS)
-> object storage -> private podcast RSS feed.

Idempotent: running it twice on the same day does nothing the second time.
"""
from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

SENDER = "crew@morningbrew.com"
KEEP_EPISODES = 14
MAX_TTS_CHARS = 3800  # OpenAI speech endpoint rejects input over 4096 chars
MAX_EMAIL_CHARS = 60_000

SCRIPT_MODEL = os.environ.get("SCRIPT_MODEL", "gpt-4.1")
TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "coral")
TTS_STYLE = (
    "Warm, upbeat morning-radio host. Conversational pace suited to someone "
    "walking. Let the dry humor land without overacting."
)

SCRIPT_PROMPT = """\
You are an EDITOR, not a writer. You turn the Morning Brew newsletter into a \
script to be read aloud to someone on a morning walk. Output ONLY the words \
to be spoken.

Fidelity rules (most important):
- Keep the newsletter's own sentences and wording. Make the smallest edits \
needed to sound natural when spoken. Do NOT summarize or paraphrase stories.
- Names, titles, and terms must appear EXACTLY as written, even if they \
differ from what you remember. The newsletter is more current than your \
knowledge. Do not add "former", change a title, or swap a name (for example \
"MS NOW" stays "MS NOW"; "President Trump" stays "President Trump").
- Never add facts, opinions, or context that are not in the newsletter.

Start with: "Good morning. It's {spoken_date}. This is your Morning Brew." \
Then go through the newsletter in order.

Keep everything editorial: the opening intro and joke, every news story in \
full, every "what else is brewing" item, every ICYMI / "Have you heard" item, \
the reader responses under Community, and the Word of the Day. The dry humor \
is the point; keep it. Turn bullet lists into a spoken sentence.

Cut entirely: anything labeled "Sponsored By" or "A message from our sponsor", \
the "In today's newsletter" list, the recs list, the reader poll, crossword, \
Open House and its answer, referral and share sections, "Submit your response" \
prompts, links, image credits and captions, navigation, social links, footer \
and unsubscribe text.

Author credits: the writers' initials or names at the end of an item (like \
"—BC") are credits. Delete them. Never speak an author's name.

Markets table: do not read the table. Speak only the newsletter's own \
"Markets:" and "Stock spotlight:" sentences, plus one short sentence on \
Bitcoin only if it moved more than 3 percent.

Write for the ear:
- No headings, markdown, bullets, or emoji. Section labels like "World" or \
"What else is brewing" may become a brief spoken lead-in ("In world news," \
"What else is brewing:") so the listener knows the topic changed.
- Keep numbers as digits ("$1.2 million", "1999", "5,500,000%"); write "%" as \
"percent" and "bps" as "basis points". Write "US" as "U.S."

End with: "That's your Morning Brew. Have a great walk."
"""


@dataclass
class Episode:
    id: str  # message-id hash, stable guid
    date: str  # YYYY-MM-DD in BREW_TZ
    title: str
    file: str  # storage key relative to the feed token dir
    bytes: int
    summary: str
    published: str  # RFC 2822


# ---------------------------------------------------------------- email ----

def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "img"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def message_body(msg: email.message.EmailMessage) -> str:
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        return html_to_text(html.get_content())
    plain = msg.get_body(preferencelist=("plain",))
    return plain.get_content().strip() if plain is not None else ""


def fetch_latest_brew(address: str, app_password: str) -> email.message.EmailMessage | None:
    """Newest email from the Morning Brew daily sender in the last 2 days."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(address, app_password)
        imap.select('"[Gmail]/All Mail"', readonly=True)
        status, data = imap.search(
            None, "X-GM-RAW", f'"from:{SENDER} newer_than:2d"'
        )
        if status != "OK" or not data[0]:
            return None
        newest = data[0].split()[-1]
        status, parts = imap.fetch(newest, "(RFC822)")
        if status != "OK":
            return None
        return email.message_from_bytes(parts[0][1], policy=email.policy.default)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# --------------------------------------------------------------- script ----

def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def spoken_date(d: datetime) -> str:
    return f"{d:%A}, {d:%B} {ordinal(d.day)}"


def unsupported_numbers(script: str, source: str) -> list[str]:
    """Numbers spoken in the script that never appear in the source email.

    A cheap guard against the LLM inventing figures; the date intro is exempt.
    """
    def nums(s: str) -> set[str]:
        return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s)}

    body = script.split("\n\n", 1)[1] if "\n\n" in script else script
    return sorted(nums(body) - nums(source))


def write_script(newsletter_text: str, when: datetime) -> str:
    from openai import OpenAI

    resp = OpenAI().chat.completions.create(
        model=SCRIPT_MODEL,
        temperature=0.2,
        messages=[
            {"role": "system", "content": SCRIPT_PROMPT.format(spoken_date=spoken_date(when))},
            {"role": "user", "content": newsletter_text[:MAX_EMAIL_CHARS]},
        ],
    )
    script = (resp.choices[0].message.content or "").strip()
    if len(script.split()) < 200:
        raise RuntimeError(f"Script suspiciously short ({len(script.split())} words):\n{script}")
    if bad := unsupported_numbers(script, newsletter_text):
        print(f"WARNING: numbers in script not found in email: {bad}", file=sys.stderr)
    return script


# ------------------------------------------------------------------ tts ----

def chunk_text(text: str, limit: int = MAX_TTS_CHARS) -> list[str]:
    """Split on paragraph, then sentence boundaries, keeping chunks under limit."""
    chunks: list[str] = []
    current = ""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if len(para) <= limit:
            units.append(para)
        else:
            units.extend(re.split(r"(?<=[.!?])\s+", para))
    for unit in units:
        # A single sentence longer than the limit: hard-split on whitespace.
        while len(unit) > limit:
            cut = unit.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            unit, rest = unit[:cut], unit[cut:].lstrip()
            chunks.append(unit)
            unit = rest
        if current and len(current) + len(unit) + 2 > limit:
            chunks.append(current)
            current = unit
        else:
            current = f"{current}\n\n{unit}" if current else unit
    if current:
        chunks.append(current)
    return chunks


def synthesize(script: str) -> bytes:
    from openai import OpenAI

    client = OpenAI()
    audio = bytearray()
    parts = chunk_text(script)
    for i, part in enumerate(parts, 1):
        print(f"  tts {i}/{len(parts)} ({len(part)} chars)", file=sys.stderr)
        resp = client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=part,
            instructions=TTS_STYLE,
            response_format="mp3",
        )
        audio += resp.content  # MP3 frames concatenate cleanly
    return bytes(audio)


# -------------------------------------------------------------- storage ----

class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def get(self, key: str) -> bytes | None:
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


class S3Storage:
    """Any S3-compatible bucket (Supabase Storage, Cloudflare R2, Backblaze B2)."""

    def __init__(self, endpoint_url: str, key_id: str, secret: str, bucket: str,
                 region: str = "auto"):
        import boto3

        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name=region,
        )

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)


def make_storage():
    if os.environ.get("S3_BUCKET"):
        require_env("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
        return S3Storage(
            os.environ["S3_ENDPOINT_URL"],
            os.environ["S3_ACCESS_KEY_ID"],
            os.environ["S3_SECRET_ACCESS_KEY"],
            os.environ["S3_BUCKET"],
            os.environ.get("S3_REGION", "auto"),
        )
    return LocalStorage(Path(os.environ.get("LOCAL_OUT", "public")))


# ----------------------------------------------------------------- feed ----

def build_feed(episodes: list[Episode], base_url: str, token: str) -> str:
    root = f"{base_url.rstrip('/')}/{token}"
    items = []
    for ep in episodes:
        items.append(f"""\
    <item>
      <title>{escape(ep.title)}</title>
      <description>{escape(ep.summary)}</description>
      <guid isPermaLink="false">{escape(ep.id)}</guid>
      <pubDate>{ep.published}</pubDate>
      <enclosure url="{escape(root)}/{escape(ep.file)}" length="{ep.bytes}" type="audio/mpeg"/>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Morning Brew, Read Aloud</title>
    <link>{escape(root)}/feed.xml</link>
    <description>A daily audio reading of the Morning Brew newsletter. Personal use.</description>
    <language>en-us</language>
    <itunes:author>Morning Brew (narrated)</itunes:author>
    <itunes:block>Yes</itunes:block>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
"""


def load_episodes(storage, token: str) -> list[Episode]:
    raw = storage.get(f"{token}/episodes.json")
    return [Episode(**e) for e in json.loads(raw)] if raw else []


def publish(storage, token: str, base_url: str, episode: Episode, audio: bytes,
            episodes: list[Episode]) -> None:
    storage.put(f"{token}/{episode.file}", audio, "audio/mpeg")
    episodes = sorted([*episodes, episode], key=lambda e: e.date, reverse=True)
    for old in episodes[KEEP_EPISODES:]:
        storage.delete(f"{token}/{old.file}")
    episodes = episodes[:KEEP_EPISODES]
    storage.put(f"{token}/episodes.json",
                json.dumps([asdict(e) for e in episodes], indent=2).encode(),
                "application/json")
    storage.put(f"{token}/feed.xml", build_feed(episodes, base_url, token).encode(),
                "application/rss+xml")


# ------------------------------------------------------------------ cli ----

def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-file", type=Path,
                    help="use this text file instead of fetching from Gmail")
    ap.add_argument("--script-only", action="store_true",
                    help="print the spoken script and stop (no TTS, no publishing)")
    ap.add_argument("--force", action="store_true",
                    help="regenerate even if today's episode already exists")
    args = ap.parse_args()

    tz = ZoneInfo(os.environ.get("BREW_TZ", "America/New_York"))

    if args.from_file:
        text = args.from_file.read_text()
        when = datetime.now(tz)
        subject = "Morning Brew"
        msg_id = f"file-{hashlib.sha1(text.encode()).hexdigest()[:12]}"
    else:
        require_env("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
        msg = fetch_latest_brew(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        if msg is None:
            sys.exit("No Morning Brew email from the last 2 days yet. Will retry on next run.")
        text = message_body(msg)
        when = email.utils.parsedate_to_datetime(msg["Date"]).astimezone(tz)
        subject = re.sub(r"^[^\w]+", "", str(msg["Subject"] or "Morning Brew")).strip()
        msg_id = hashlib.sha1(str(msg["Message-ID"]).encode()).hexdigest()[:16]

    if not text:
        sys.exit("Email had no readable body.")

    require_env("OPENAI_API_KEY")
    if args.script_only:
        print(write_script(text, when))
        return

    require_env("FEED_TOKEN", "PUBLIC_BASE_URL")
    token = os.environ["FEED_TOKEN"]
    storage = make_storage()
    episodes = load_episodes(storage, token)
    if not args.force and any(e.id == msg_id for e in episodes):
        print("Already published today's episode.")
        return
    episodes = [e for e in episodes if e.id != msg_id]

    print("Writing script...", file=sys.stderr)
    script = write_script(text, when)
    print("Synthesizing audio...", file=sys.stderr)
    audio = synthesize(script)

    date = f"{when:%Y-%m-%d}"
    episode = Episode(
        id=msg_id,
        date=date,
        title=f"{when:%b} {when.day}: {subject}",
        file=f"episodes/{date}.mp3",
        bytes=len(audio),
        summary=script.split("\n\n")[1][:300] if "\n\n" in script else script[:300],
        published=email.utils.format_datetime(when),
    )
    publish(storage, token, os.environ["PUBLIC_BASE_URL"], episode, audio, episodes)
    print(f"Published {episode.title} ({len(audio) / 1e6:.1f} MB)")
    print(f"Feed: {os.environ['PUBLIC_BASE_URL'].rstrip('/')}/{token}/feed.xml")


if __name__ == "__main__":
    main()
