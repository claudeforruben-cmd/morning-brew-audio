#!/usr/bin/env python3
"""Turn newsletter emails (Morning Brew, MarketWatch, WSJ) into podcast episodes.

Pipeline: Gmail (IMAP) -> plain text -> spoken script (LLM) -> MP3 (TTS)
-> object storage -> one private podcast RSS feed per newsletter.

Idempotent: an email that already has an episode is never processed twice, and
an email that arrives late is picked up on the next run.
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

KEEP_EPISODES = 14
LOOKBACK_DAYS = 2  # how far back to look for emails that have no episode yet
MAX_NEW_PER_RUN = 3  # newest N unpublished emails per source per run
MAX_TTS_CHARS = 3800  # OpenAI speech endpoint rejects input over 4096 chars
MAX_EMAIL_CHARS = 60_000

# Free by default: Gemini free tier for the script, Microsoft Edge neural voices
# for speech. Set SCRIPT_PROVIDER=openai / TTS_PROVIDER=openai for the paid ones.
SCRIPT_PROVIDER = os.environ.get("SCRIPT_PROVIDER", "gemini")
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "edge")
SCRIPT_MODEL = os.environ.get("SCRIPT_MODEL", "")  # empty = provider default
# Gemini model names get retired and free-tier quota differs per model, so try
# these in order until one answers. "gemini-flash-latest" is Google's alias.
GEMINI_MODELS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash",
                 "gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
GEMINI_SKIP_STATUSES = {404, 429, 500, 503}  # model missing, no quota, or overloaded
TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "coral")  # OpenAI voice
EDGE_VOICE = os.environ.get("EDGE_VOICE", "en-US-AndrewMultilingualNeural")
EDGE_RATE = os.environ.get("EDGE_RATE", "+5%")
TTS_STYLE = (
    "Warm, upbeat morning-radio host. Conversational pace suited to someone "
    "walking. Let the dry humor land without overacting."
)

BREW_PROMPT = """\
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

MARKETS_PROMPT = """\
You are an EDITOR, not a writer. You turn the {publication} email newsletter \
into a script to be read aloud to someone listening on the go. Output ONLY the \
words to be spoken.

Fidelity rules (most important):
- Keep the newsletter's own sentences and wording. Make the smallest edits \
needed to sound natural when spoken. Do NOT summarize or paraphrase stories.
- Names, titles, tickers, and terms must appear EXACTLY as written, even if \
they differ from what you remember. The newsletter is more current than your \
knowledge. Do not add "former", change a title, or swap a name.
- Never add facts, opinions, or context that are not in the newsletter.

Start with: "It's {spoken_date}. This is your {publication} briefing." Then go \
through the newsletter in order.

Keep everything editorial: every story, summary, analysis, and market-moves \
paragraph, in full. Turn bullet lists into spoken sentences.

Cut entirely: anything sponsored or advertising ("Presented by", "Sponsored", \
"Advertisement", "Partner content"), promotions and subscription or app offers, \
"sign up" and "share this" prompts, plugs for other newsletters, polls, \
quizzes, puzzles, links, photo and image credits and captions, navigation, \
social links, footer, legal, and unsubscribe text.

Bylines: writers' names and credits are not spoken. Delete them, unless the \
person is quoted or is the subject of a story.

Tables and tickers: do not read data tables or lists of quotes. Speak only the \
newsletter's own sentences about the markets.

Write for the ear:
- No headings, markdown, bullets, or emoji. Section labels may become a brief \
spoken lead-in ("In the markets,") so the listener knows the topic changed.
- Keep numbers as digits ("$1.2 million", "1999"); write "%" as "percent" and \
"bps" as "basis points". Write "US" as "U.S."

End with: "That's your {publication} briefing."
"""


@dataclass(frozen=True)
class Source:
    key: str  # --source name, GitHub Actions input
    show: str  # podcast title
    publication: str  # spoken name
    senders: tuple[str, ...]  # Gmail from: matches (address or domain)
    prompt: str
    subdir: str = ""  # feed folder under the token; "" keeps the original URL
    min_words: int = 120  # a script shorter than this means something went wrong
    # Welcome and confirmation emails are not editions.
    skip_subject: str = r"\b(welcome|confirm|verify|thanks for signing)\b"


SOURCES = {s.key: s for s in (
    Source("brew", "Morning Brew, Read Aloud", "Morning Brew",
           ("crew@morningbrew.com",), BREW_PROMPT, min_words=200),
    Source("marketwatch", "MarketWatch, Read Aloud", "MarketWatch",
           # The Midday Report comes from reports@marketwatchmail.com.
           ("marketwatchmail.com", "marketwatch.com"), MARKETS_PROMPT,
           subdir="marketwatch"),
    Source("wsj", "Wall Street Journal, Read Aloud", "The Wall Street Journal",
           ("wsj.com",), MARKETS_PROMPT, subdir="wsj"),
)}


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


class Mailbox:
    """Read-only Gmail over IMAP. Message numbers are stable for the session."""

    def __init__(self, address: str, app_password: str):
        self.imap = imaplib.IMAP4_SSL("imap.gmail.com")
        self.imap.login(address, app_password)
        self.imap.select('"[Gmail]/All Mail"', readonly=True)

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass

    def search(self, senders: tuple[str, ...], days: int = LOOKBACK_DAYS) -> list[bytes]:
        """Emails from any of these senders in the last N days, oldest first."""
        frm = senders[0] if len(senders) == 1 else f"({' OR '.join(senders)})"
        status, data = self.imap.search(None, "X-GM-RAW", f'"from:{frm} newer_than:{days}d"')
        return data[0].split() if status == "OK" and data[0] else []

    def _fetch(self, num: bytes, what: str) -> email.message.EmailMessage:
        status, parts = self.imap.fetch(num, what)
        if status != "OK":
            raise RuntimeError(f"IMAP fetch {what} failed for message {num!r}")
        return email.message_from_bytes(parts[0][1], policy=email.policy.default)

    def headers(self, num: bytes) -> email.message.EmailMessage:
        return self._fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])")

    def message(self, num: bytes) -> email.message.EmailMessage:
        return self._fetch(num, "(RFC822)")


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


def _gemini_call(model: str, system: str, user: str) -> str:
    import urllib.request

    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2},
        }).encode(),
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.load(resp)
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def _llm_gemini(system: str, user: str) -> str:
    import urllib.error

    failures = []
    for model in [SCRIPT_MODEL] if SCRIPT_MODEL else GEMINI_MODELS:
        try:
            text = _gemini_call(model, system, user)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:400]
            print(f"  gemini {model}: HTTP {e.code} {body}", file=sys.stderr)
            failures.append(f"{model}: HTTP {e.code}")
            if e.code not in GEMINI_SKIP_STATUSES:
                raise  # bad key, bad request, etc.: another model won't help
            continue
        if text.strip():
            print(f"  script written by {model}", file=sys.stderr)
            return text
        failures.append(f"{model}: empty response")
    raise RuntimeError("No Gemini model produced a script: " + "; ".join(failures))


def _llm_openai(system: str, user: str) -> str:
    from openai import OpenAI

    resp = OpenAI().chat.completions.create(
        model=SCRIPT_MODEL or "gpt-4.1",
        temperature=0.2,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or ""


def write_script(src: Source, newsletter_text: str, when: datetime) -> str:
    llm = _llm_gemini if SCRIPT_PROVIDER == "gemini" else _llm_openai
    system = src.prompt.format(spoken_date=spoken_date(when), publication=src.publication)
    script = llm(system, newsletter_text[:MAX_EMAIL_CHARS]).strip()
    if len(script.split()) < src.min_words:
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


def synthesize_edge(script: str) -> bytes:
    import asyncio

    import edge_tts

    async def run() -> bytes:
        audio = bytearray()
        stream = edge_tts.Communicate(script, EDGE_VOICE, rate=EDGE_RATE).stream()
        async for chunk in stream:
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return bytes(audio)

    audio = asyncio.run(run())
    if len(audio) < 100_000:
        raise RuntimeError(f"Edge TTS returned only {len(audio)} bytes of audio")
    return audio


def synthesize_openai(script: str) -> bytes:
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


def synthesize(script: str) -> bytes:
    return synthesize_edge(script) if TTS_PROVIDER == "edge" else synthesize_openai(script)


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
        # List first: some S3-compatible stores (Supabase) answer a GET for a
        # missing key with a JSON error botocore can't parse, so it surfaces as
        # a ClientError with a blank code. A listing never has that ambiguity.
        listing = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=key, MaxKeys=1)
        if not any(o["Key"] == key for o in listing.get("Contents", [])):
            return None
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

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

def build_feed(episodes: list[Episode], base_url: str, prefix: str,
               show: str = "Morning Brew, Read Aloud",
               publication: str = "Morning Brew") -> str:
    root = f"{base_url.strip().rstrip('/')}/{prefix.strip()}"
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
    <title>{escape(show)}</title>
    <link>{escape(root)}/feed.xml</link>
    <description>An audio reading of the {escape(publication)} newsletter. Personal use.</description>
    <language>en-us</language>
    <itunes:author>{escape(publication)} (narrated)</itunes:author>
    <itunes:block>Yes</itunes:block>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
"""


def load_episodes(storage, prefix: str) -> list[Episode]:
    raw = storage.get(f"{prefix}/episodes.json")
    return [Episode(**e) for e in json.loads(raw)] if raw else []


def publish(storage, prefix: str, base_url: str, episode: Episode, audio: bytes,
            episodes: list[Episode], show: str = "Morning Brew, Read Aloud",
            publication: str = "Morning Brew") -> list[Episode]:
    """Store the episode, rewrite the index and feed, and return the kept episodes."""
    storage.put(f"{prefix}/{episode.file}", audio, "audio/mpeg")
    episodes = sorted([*episodes, episode], key=lambda e: e.date, reverse=True)
    for old in episodes[KEEP_EPISODES:]:
        storage.delete(f"{prefix}/{old.file}")
    episodes = episodes[:KEEP_EPISODES]
    storage.put(f"{prefix}/episodes.json",
                json.dumps([asdict(e) for e in episodes], indent=2).encode(),
                "application/json")
    storage.put(f"{prefix}/feed.xml",
                build_feed(episodes, base_url, prefix, show, publication).encode(),
                "application/rss+xml")
    return episodes


# ------------------------------------------------------------------ cli ----

def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")


def clean_subject(subject: object) -> str:
    """Drop the leading emoji Morning Brew puts on every subject."""
    return re.sub(r"^[^\w]+", "", str(subject or "")).strip()


def make_episode(src: Source, storage, prefix: str, base_url: str,
                 episodes: list[Episode], msg_id: str, when: datetime,
                 subject: str, text: str) -> list[Episode]:
    """Write the script, voice it, and publish. Returns the updated episode list."""
    print(f"  [{src.key}] writing script...", file=sys.stderr)
    script = write_script(src, text, when)
    print(f"  [{src.key}] synthesizing audio...", file=sys.stderr)
    audio = synthesize(script)

    date = f"{when:%Y-%m-%d}"
    episode = Episode(
        id=msg_id,
        date=date,
        title=f"{when:%b} {when.day}: {subject or src.publication}",
        file=f"episodes/{date}-{msg_id[:6]}.mp3",
        bytes=len(audio),
        summary=script.split("\n\n")[1][:300] if "\n\n" in script else script[:300],
        published=email.utils.format_datetime(when),
    )
    episodes = [e for e in episodes if e.id != msg_id]
    episodes = publish(storage, prefix, base_url, episode, audio, episodes,
                       src.show, src.publication)
    print(f"[{src.key}] published {episode.title} ({len(audio) / 1e6:.1f} MB)")
    print(f"[{src.key}] feed: {base_url.rstrip('/')}/{prefix}/feed.xml")
    return episodes


def run_source(src: Source, box: Mailbox, storage, token: str, base_url: str,
               tz: ZoneInfo, force: bool = False) -> int:
    """Publish every email from this source that has no episode yet. Returns the count."""
    prefix = "/".join(p for p in (token, src.subdir) if p)
    episodes = load_episodes(storage, prefix)
    first_run = not episodes
    known_ids = {e.id for e in episodes}
    # The same email can arrive twice (forwarded and direct); match on title too.
    known_titles = {e.title for e in episodes}

    matches = box.search(src.senders)
    candidates = []  # oldest first
    for num in matches:
        hdr = box.headers(num)
        subject = clean_subject(hdr["Subject"])
        if re.search(src.skip_subject, subject, re.I):
            print(f"[{src.key}] skipping non-edition email: {subject!r}")
            continue
        when = email.utils.parsedate_to_datetime(hdr["Date"]).astimezone(tz)
        msg_id = hashlib.sha1(str(hdr["Message-ID"] or f"{subject}{hdr['Date']}").encode()
                              ).hexdigest()[:16]
        title = f"{when:%b} {when.day}: {subject or src.publication}"
        candidates.append((num, msg_id, when, subject, title))

    if force:
        todo = candidates[-1:]  # regenerate the newest even though it exists
    else:
        todo = [c for c in candidates if c[1] not in known_ids and c[4] not in known_titles]
        if first_run:
            todo = todo[-1:]  # a new feed starts with the latest, not a backlog
        todo = todo[-MAX_NEW_PER_RUN:]

    newest = candidates[-1][4] if candidates else "none"
    print(f"[{src.key}] {len(matches)} email(s) in the last {LOOKBACK_DAYS} days, "
          f"{len(todo)} new; newest: {newest}")

    published = 0
    for num, msg_id, when, subject, _ in todo:
        text = message_body(box.message(num))
        if not text:
            print(f"[{src.key}] email {subject!r} had no readable body; skipping")
            continue
        episodes = make_episode(src, storage, prefix, base_url, episodes, msg_id,
                                when, subject, text)
        published += 1

    if not published and episodes:
        # Cheap and self-healing: settings like the base URL apply without a new episode.
        storage.put(f"{prefix}/feed.xml",
                    build_feed(episodes, base_url, prefix, src.show, src.publication).encode(),
                    "application/rss+xml")
    return published


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["all", *SOURCES], default="all",
                    help="which newsletter to process (default: all)")
    ap.add_argument("--from-file", type=Path,
                    help="use this text file instead of fetching from Gmail")
    ap.add_argument("--script-only", action="store_true",
                    help="print the spoken script and stop (no TTS, no publishing)")
    ap.add_argument("--force", action="store_true",
                    help="regenerate the newest episode even if it already exists")
    args = ap.parse_args()

    tz = ZoneInfo(os.environ.get("BREW_TZ", "America/New_York"))
    sources = list(SOURCES.values()) if args.source == "all" else [SOURCES[args.source]]

    require_env(*(["GEMINI_API_KEY"] if SCRIPT_PROVIDER == "gemini" else ["OPENAI_API_KEY"]))
    if TTS_PROVIDER == "openai":
        require_env("OPENAI_API_KEY")

    if args.from_file:
        src = sources[0] if len(sources) == 1 else SOURCES["brew"]
        text = args.from_file.read_text()
        when = datetime.now(tz)
        if args.script_only:
            print(write_script(src, text, when))
            return
        require_env("FEED_TOKEN", "PUBLIC_BASE_URL")
        token = os.environ["FEED_TOKEN"].strip()
        prefix = "/".join(p for p in (token, src.subdir) if p)
        storage = make_storage()
        msg_id = f"file-{hashlib.sha1(text.encode()).hexdigest()[:12]}"
        make_episode(src, storage, prefix, os.environ["PUBLIC_BASE_URL"].strip(),
                     load_episodes(storage, prefix), msg_id, when, src.publication, text)
        return

    require_env("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "FEED_TOKEN", "PUBLIC_BASE_URL")
    token = os.environ["FEED_TOKEN"].strip()
    base_url = os.environ["PUBLIC_BASE_URL"].strip()  # pasted secrets often carry stray spaces
    storage = make_storage()
    box = Mailbox(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
    failures = []
    try:
        for src in sources:
            # One newsletter failing must not block the others.
            try:
                run_source(src, box, storage, token, base_url, tz, args.force)
            except Exception as e:
                print(f"[{src.key}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
                failures.append(src.key)
    finally:
        box.close()
    if failures:
        sys.exit(f"Failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
