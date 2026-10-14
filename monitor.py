#!/usr/bin/env python3
"""
ig-pulse monitor — continuous Instagram hashtag-spike radar.

Runs every tick (LaunchAgent, ~30 min). Pure stdlib, zero model cost.
Polls the active hashtag pool via the Graph API, snapshots per-post
like/comment counts, computes velocity + acceleration, and when a reel
crosses the fire threshold: downloads the clip, writes a job file, and
spawns the pulse architect (unless inside the daily-routine quiet window,
in which case the job queues and a later tick spawns it).

State lives in state/ as JSON. Kill switch: a PAUSED file in the repo root.
Test: python3 monitor.py --dry-run   (polls the LIVE API — quota-neutral on
existing tags — but never fires, spawns, or writes state)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
JOBS = ROOT / "jobs"
CLIPS = ROOT / "clips"
LOGS = ROOT / "logs"

ANCHOR = "17841480076308621"  # imabgold IG user id — the only sanctioned anchor node
GRAPH = "https://graph.facebook.com/v21.0"
TOKEN_ENV = Path.home() / "Claude Code" / "reel-app" / ".env"
BANK_LOGS = Path.home() / "Claude Code" / "viral-bank" / "logs"

CFG_DEFAULTS = {
    "weekly_quota": 30,          # Meta: unique hashtags per rolling 7 days (shared with bank scan)
    "bank_reserve": 6,           # headroom left for the Mon/Thu bank scan's own new tags
    "fire_floor_lph": 300,       # likes/hour minimum to fire
    "near_spike_factor": 0.6,    # rate >= floor*this feeds tag expansion + keeps tag alive
    "accel_min": 1.25,           # rate acceleration multiplier that qualifies without top_media
    "comments_to_likes": 25,     # proxy multiplier when likes are hidden
    "max_age_hours": 72,         # ignore posts older than this
    "min_snap_gap_hours": 0.4,   # DUAL ROLE (audit 2026-07-28): scoring guard in rate_between AND
    # the snapshot() dedupe gate — a sighting closer than this to the last
    # snap is NOT recorded. Safe because launchd ticks are exit-anchored
    # (>=0.5h apart). Raising this to >=0.5 would silently stop all snaps
    # after the first and the monitor would never fire again; the queued
    # 15-min tick rhythm (TODO.md) must lower it to ~0.2 in the same change.
    "tag_cooldown_min": 120,     # min gap between fires from the same tag
    "tag_evict_days": 14,        # caption-sourced tags with no near-spike this long get evicted
    "max_pool": 12,              # hard cap on ACTIVE tags — Graph rate-limit safety, independent of quota
    # 2026-07-29, Marcus: caption expansion OFF (0 = disabled) — it dragged
    # #risadas/#foodie/#lunch into the pool; the pool is now hand-curated
    # around AI + the profile's own hashtags. Pending curated tags in
    # tags.json ("pending": true) phase in one per tick as quota frees.
    "max_tag_adds_per_tick": 0,  # expansion trickle, never a caption-dump flood
    "tag_add_min_captions": 2,   # a tag must appear in >=2 distinct near-spike captions — one viral lunch reel must not drag the pond off-brand
    "max_dl_fails": 3,           # give up firing a media after this many failed clip downloads
    # --- pre-fire triage (2026-07-27, from architect data: 0/15 refs >30s
    # ever survived triage; 4 of the last 9 had zero scene cuts = nothing to
    # mimic). Runs on the downloaded clip BEFORE spawning an opus architect.
    # 2026-07-29, Marcus: cap lowered 30->22 — every 22-30s ref (4 jobs) hard-
    # declined on multiple gates; the only near-miss ever was 20.7s. ---
    "prefilter_max_s": 22,       # suppress refs longer than this outright
    "prefilter_scene_min_s": 10, # refs longer than this need >=1 scene cut
    "prefilter_scene_thresh": 0.1,
    "max_stale_hours": 4,        # a media unseen this long can't fire — covers one deep-sweep cycle (~3.5h), beyond that its score is frozen history
    "quiet_window": ["08:25", "10:35"],  # daily-routine window: queue fires, don't spawn
    "recent_limit": 30,
    "top_limit": 20,
    "max_snaps": 12,
    "min_pool": 6,
    # --- adaptive pagination (recent_media is a paginated feed; page one on a
    # busy tag covers ~30 min, so posts vanished before their 3rd snapshot) ---
    "call_budget_per_tick": 95,  # ceiling on Graph calls per tick (2 ticks/hr -> ~190/hr, under Meta's ~200; raised from 90 on 2026-07-28 to fund firehose deep sweeps)
    "max_pages_per_tag": 8,      # recent_media pages per tag per tick (fresh sweep)
    "page_horizon_hours": 6,     # fresh sweep: stop paging once a page reaches posts older than this
    # --- two-tier sweep: every tick short-scrolls all tags to 6h; ONE tag per
    # tick (rotating, least-recently-deep first) long-scrolls toward 24h so
    # slow-burn risers between 6h and 24h stay measurable (~every 3.5h) ---
    "deep_horizon_hours": 24,    # deep sweep: page until posts are older than this
    "deep_page_cap": 28,         # rotating deep-sweep call cap (non-firehose tags)
    # --- firehose mode (2026-07-28, Marcus): tags flagged "firehose" in
    # tags.json get a deep sweep EVERY tick (subsumes their fresh sweep)
    # instead of joining the rotation. On a tag posting hundreds of reels/hr,
    # re-finding a reel 3 times in ~90 min is only possible by long-scrolling
    # every tick; reels older than the reachable window stay invisible — this
    # mode catches fast young risers only, which is what pulse is for.
    # VERDICT 2026-07-29, Marcus: RETIRED for true firehoses. #ai at 18 pages
    # got WORSE observability than rotation (0.8% of 25.5k reels ever sighted
    # 3x, best rate 42 l/hr, 0 fires in 6 days) while its 32 off-the-top calls
    # measurably starved the producing tags (#buildinpublic 25%->14%,
    # #techtok 93%->87% 3-sighting rates). A tag that outposts the paging
    # window cannot be observed at any affordable cap — do NOT re-flag broad
    # tags. Machinery kept (dormant while no active tag carries the flag);
    # the caption mini-chain idea was validated and lives on below. ---
    "firehose_page_cap": 18,      # caption-less deep chain (funds the caption chain below)
    "firehose_caption_pages": 12, # CAPTIONED mini-chain per firehose tick. SIZING RULE (audit
    # 2026-07-28 round 3): captions persist from DISCOVERY (setdefault never
    # recreates, captions never cleared), so this chain must cover ONE TICK
    # GAP of new posts — pages x page_limit >= posting_rate x ~0.6h, with
    # margin. #ai: 12 x 15 = 180 posts vs ~262/hr x 0.6 = ~157. A reel first
    # discovered by the caption-less deep chain on a firehose tag stays blind
    # forever (no fresh sweep to backfill) — then PULSE.md tells the
    # architect to read the caption from the reel's public permalink page.
}

BAIT_RE = re.compile(
    r"giveaway|tag a friend|tag \d|to win|follow to enter|link in bio to claim"
    # 2026-07-29 (architect, job 20260729-022123): comment-to-get gates —
    # 'comment "AI" and I'll DM you the link' — spike on comment velocity,
    # never on reach, and are an automatic step-1 decline downstream
    r"|comment\s+[\"'“”]?\w{1,20}[\"'“”]?\s+(and i|to get|for the|below to)"
    r"|comment .{0,40}\b(dm|link|send)|i'?ll dm you", re.I
)
# 2026-07-29, Marcus: 8 of the first 21 architect runs burned on pt-BR /
# Hindi references the brand can never voice — suppress before the download.
# Non-Latin scripts (Devanagari, Arabic, CJK, Hangul, Cyrillic, Thai, kana)
# or 2+ distinct Portuguese/Spanish/romanized-Hindi function words = skip.
NON_LATIN_RE = re.compile(
    "[\\u0400-\\u04FF\\u0590-\\u05FF\\u0600-\\u06FF\\u0900-\\u097F"
    "\\u0E00-\\u0E7F\\u3040-\\u30FF\\u3400-\\u9FFF\\uAC00-\\uD7AF]")
NON_EN_WORDS_RE = re.compile(
    r"\b(voc[eê]|n[aã]o|est[aá]|para|porque|quando|muito|isso|"
    r"pra|meu|minha|vou|tudo|mais|filho|m[aã]e|esse|essa|"
    r"pero|cuando|tambi[eé]n|hasta|ahora|"
    r"nahi|kaise|karo|kamai|paisa)\b", re.I)
# 2026-07-29 (architect, both #techtok hardware jobs): formats gated on gear
# the account does not own — well-made but never suppliable.
GEAR_RE = re.compile(
    # audit 2026-07-29: gpu/graphics card/rgb/nas dropped — bare #GPU in
    # AI-news hashtag blocks was 93% of matches, killing the exact genre the
    # pool exists to find; keep only terms that name a gear-flex FORMAT
    r"\b(monitor|oled|rtx|mechanical keyboard|"
    r"battlestation|pc build|gaming setup|setup tour|ultrawide|homelab)\b",
    re.I)
MEGATAGS = {
    "viral", "fyp", "fypage", "foryou", "foryoupage", "explore", "explorepage",
    "reels", "reel", "reelsinstagram", "instagram", "instagood", "trending",
    "viralreels", "reelsviral", "love", "follow", "like", "video", "meme",
    # generic "laughs" tags — 2026-07-27, Marcus: #risadas burned 7 architect
    # runs at 0% yield (licensed reposts, discourse bait, local-biz skits);
    # broad humor tags surface nothing structurally portable
    "risadas", "humor", "trendingreels", "engracado", "comedia", "funny",
    # 2026-07-28, Marcus: #memes dropped — firehose (15,790 tracked, 0.2%
    # ever observable long enough to fire) and same genre as the laughs purge
    "memes",
}
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{3,30})")


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def log(msg):
    line = f"{datetime.now().strftime('%F %T')} {msg}"
    print(line)
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / f"{datetime.now():%F}.log", "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    tmp.replace(path)


def token():
    for line in TOKEN_ENV.read_text().splitlines():
        if line.startswith("META_SYSTEM_USER_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"META_SYSTEM_USER_TOKEN missing in {TOKEN_ENV}")


def api_get(path, params, tok, retries=2, full_url=None):
    if full_url:
        url = full_url  # a paging "next" link — embeds the token, NEVER log it
    else:
        params = dict(params, access_token=tok)
        url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            # "reduce the amount of data" — page too heavy, caller must shrink
            # the limit; retrying the same request just burns 45s
            if "reduce the amount" in body.lower():
                return {"error": {"http": e.code, "body": body, "too_heavy": True}}
            # 4xx = permanent (bad field / permission / quota) — surface, don't retry
            if 400 <= e.code < 500:
                return {"error": {"http": e.code, "body": body}}
            if attempt == retries:
                return {"error": {"http": e.code, "body": body}}
        except Exception as e:  # network blips
            if attempt == retries:
                return {"error": {"transport": str(e)[:200]}}
        time.sleep(5 * (attempt + 1))


def bank_charged_tags(days=7):
    """Unique hashtags the Mon/Thu bank scan charged in the last `days` days."""
    tags = set()
    cutoff = datetime.now() - timedelta(days=days)
    if not BANK_LOGS.is_dir():
        return tags
    for f in BANK_LOGS.glob("*.log"):
        try:
            d = datetime.strptime(f.stem, "%Y-%m-%d")
        except ValueError:
            continue
        if d < cutoff:
            continue
        for m in re.finditer(r"hashtag-query:\s*#([A-Za-z0-9_]+)", f.read_text(errors="replace")):
            tags.add(m.group(1).lower())
    return tags


def in_quiet_window(cfg):
    lo, hi = cfg["quiet_window"]
    t = datetime.now().strftime("%H:%M")
    return lo <= t <= hi


def hhmm_ago(hours):
    return now() - timedelta(hours=hours)


class Monitor:
    def __init__(self, dry_run=False):
        self.dry = dry_run
        self.cfg = {**CFG_DEFAULTS, **load_json(ROOT / "config.json", {})}
        self.tags = load_json(STATE / "tags.json", {"pool": []})
        self.pulse = load_json(STATE / "pulse.json", {"media": {}})
        self.tok = token()
        self.fired_this_tick = 0

    # ---------- quota ----------
    def charged_7d(self):
        """All unique tags charged against the rolling-7-day quota: bank's + ours.
        Every ACTIVE tag counts even before its first poll — it WILL be polled
        next tick, so treating it as uncharged would let expansion over-subscribe
        the quota by a full tick (audit finding #1). Tombstones count until
        their last poll rolls off the 7-day window."""
        cutoff = now() - timedelta(days=7)
        ours = set()
        for t in self.tags["pool"]:
            if t.get("active"):
                ours.add(t["tag"])
            elif t.get("last_polled") and parse_iso(t["last_polled"]) >= cutoff:
                ours.add(t["tag"])
        return bank_charged_tags() | ours

    def quota_headroom(self):
        return self.cfg["weekly_quota"] - self.cfg["bank_reserve"] - len(self.charged_7d())

    # ---------- polling ----------
    def resolve_hid(self, tag):
        r = api_get("ig_hashtag_search", {"user_id": ANCHOR, "q": tag["tag"]}, self.tok)
        if r.get("data"):
            tag["hid"] = r["data"][0]["id"]
            return True
        log(f"  hid-resolve FAILED for #{tag['tag']}: {r.get('error')}")
        return False

    def first_page(self, tag, edge, limit, fields, persist_limit=True):
        """One budgeted call, shrinking the page on Graph's weight push-back.
        A working shrunk size is remembered on the tag so heavy tags don't
        re-discover it (and re-burn calls) every tick — except on deep sweeps
        (persist_limit=False), which deliberately run big pages for reach and
        must not overwrite the fresh sweep's learned size. Returns (response, calls_used)."""
        used = 0
        r = {"error": {"body": f"limit {limit} below minimum page size"}}
        while limit >= 8:
            used += 1
            r = api_get(f"{tag['hid']}/{edge}",
                        {"user_id": ANCHOR, "fields": fields, "limit": limit}, self.tok)
            if not (isinstance(r.get("error"), dict) and r["error"].get("too_heavy")):
                break
            limit //= 2  # Graph pushed back on page weight — shrink and retry
        if edge == "recent_media" and "error" not in r and persist_limit:
            tag["page_limit"] = limit
        return r, used

    def poll_tag(self, tag, allowance, deep=False):
        """Poll one tag within a call allowance. top_media gets one page;
        recent_media paginates until allowance, page cap, or the horizon —
        page one alone covers ~30 min on a busy tag, which starved reels of
        the 3 snapshots the fire gate needs. A deep sweep pages toward
        deep_horizon_hours instead (it subsumes the fresh sweep for that
        tag this tick). Returns calls used."""
        horizon_hours = self.cfg["deep_horizon_hours"] if deep else self.cfg["page_horizon_hours"]
        if deep:
            # audit 2026-07-28 finding 2: firehose tags get their own cap
            page_cap = self.cfg["firehose_page_cap"] if tag.get("firehose") else self.cfg["deep_page_cap"]
        else:
            page_cap = self.cfg["max_pages_per_tag"]
        used = 0
        if not tag.get("hid"):
            used += 1
            if not self.resolve_hid(tag):
                return used
        fields = "id,media_type,media_product_type,like_count,comments_count,caption,permalink,timestamp,media_url"
        if deep:
            # deep sweeps are stats re-reads of reels the fresh sweep already
            # captioned — dropping the caption (the page-weight driver) lets
            # Graph accept full-size pages, roughly doubling deep reach
            deep_fields = fields.replace("caption,", "")

        top_ids = set()
        r, u = self.first_page(tag, "top_media", self.cfg["top_limit"], fields)
        used += u
        if "error" in r:
            log(f"  #{tag['tag']}/top_media error: {json.dumps(r['error'])[:200]}")
        else:
            for m in r.get("data", []):
                if m.get("media_product_type") == "REELS":
                    top_ids.add(m["id"])
                    self.snapshot(tag["tag"], m, True)

        if deep and used < allowance:
            # audit 2026-07-28 finding 1 (round 2): the caption-less deep
            # chain (below) inherits its fields through paging.next, so
            # deep-discovered reels never get a caption — and firehose tags
            # never fresh-sweep to backfill one. Run a CAPTIONED mini-chain
            # over the newest posts first: firehose_caption_pages pages for
            # firehose tags — sized to cover one full tick gap of new posts,
            # because captions persist from discovery (median fire age is
            # ~12h; what matters is captioning a reel ONCE, when it first
            # appears). One page for rotation deeps. Fired jobs can still
            # carry caption "" — PULSE.md's fallback covers that.
            cap_pages = self.cfg["firehose_caption_pages"] if tag.get("firehose") else 1
            cap_horizon = now() - timedelta(hours=self.cfg["page_horizon_hours"])
            cap_next = None
            for cpage in range(cap_pages):
                if used >= allowance:
                    break
                if cpage == 0:
                    r, u = self.first_page(tag, "recent_media",
                                           tag.get("page_limit") or self.cfg["recent_limit"],
                                           fields, persist_limit=False)
                    used += u
                else:
                    used += 1
                    r = api_get(None, None, self.tok, full_url=cap_next)
                if "error" in r:
                    log(f"  #{tag['tag']}/caption-chain p{cpage + 1} error: {json.dumps(r['error'])[:200]}")
                    break
                oldest = None
                for m in r.get("data", []):
                    if m.get("media_product_type") == "REELS":
                        self.snapshot(tag["tag"], m, m["id"] in top_ids)
                    try:
                        ts = parse_iso(m["timestamp"])
                        oldest = ts if oldest is None or ts < oldest else oldest
                    except (KeyError, ValueError):
                        pass
                if oldest is not None and oldest < cap_horizon:
                    break
                cap_next = r.get("paging", {}).get("next")
                if not cap_next:
                    break

        horizon = now() - timedelta(hours=horizon_hours)
        next_url = None
        for page in range(page_cap):
            if used >= allowance:
                log(f"  #{tag['tag']}: allowance hit at page {page} — deeper posts unseen this tick")
                break
            if page == 0:
                # deep sweeps run full-size pages for reach and never persist;
                # fresh sweeps use the tag's learned size
                start_limit = self.cfg["recent_limit"] if deep else (tag.get("page_limit") or self.cfg["recent_limit"])
                r, u = self.first_page(tag, "recent_media", start_limit,
                                       deep_fields if deep else fields,
                                       persist_limit=not deep)
                used += u
            else:
                used += 1
                r = api_get(None, None, self.tok, full_url=next_url)
            if "error" in r:
                # log the error body only — a paged URL embeds the token
                log(f"  #{tag['tag']}/recent_media p{page + 1} error: {json.dumps(r['error'])[:200]}")
                if isinstance(r["error"], dict) and r["error"].get("too_heavy") and not deep:
                    # deep pages of this tag run heavy — start smaller next tick
                    tag["page_limit"] = max(8, (tag.get("page_limit") or self.cfg["recent_limit"]) // 2)
                break
            oldest = None
            for m in r.get("data", []):
                if m.get("media_product_type") == "REELS":
                    self.snapshot(tag["tag"], m, m["id"] in top_ids)
                try:
                    ts = parse_iso(m["timestamp"])
                    oldest = ts if oldest is None or ts < oldest else oldest
                except (KeyError, ValueError):
                    pass
            if oldest is not None and oldest < horizon:
                break  # this page already reaches past the spike window
            next_url = r.get("paging", {}).get("next")
            if not next_url:
                break
        tag["last_polled"] = iso(now())
        return used

    def snapshot(self, tagname, m, in_top):
        try:
            posted = parse_iso(m["timestamp"])
        except (KeyError, ValueError):
            return
        if posted < hhmm_ago(self.cfg["max_age_hours"]):
            return
        rec = self.pulse["media"].setdefault(m["id"], {
            "tag": tagname,
            "permalink": m.get("permalink"),
            "posted": m["timestamp"],
            "caption": (m.get("caption") or "")[:1000],
            "first_seen": iso(now()),
            "snaps": [],
            "fired": False,
        })
        rec["in_top"] = in_top or rec.get("in_top", False)
        if m.get("caption") and not rec.get("caption"):
            rec["caption"] = m["caption"][:1000]  # backfill a deep-discovered reel's caption on its first fresh sighting
        if m.get("media_url"):
            rec["media_url"] = m["media_url"]  # keep freshest — these URLs expire
        # sub-gap re-sighting (caption pre-pass / top_media overlap): keep the
        # metadata updates above but skip the snap — a second snap seconds
        # after the last is unscoreable (rate_between returns None below
        # min_snap_gap_hours) and pushes accel out of reach for ~2 ticks on
        # exactly the young reels firehose mode exists to catch (audit
        # 2026-07-28 round 2)
        if rec["snaps"] and (now() - parse_iso(rec["snaps"][-1]["t"])).total_seconds() / 3600 < self.cfg["min_snap_gap_hours"]:
            return
        rec["snaps"].append({
            "t": iso(now()),
            "likes": m.get("like_count"),
            "comments": m.get("comments_count"),
        })
        rec["snaps"] = rec["snaps"][-self.cfg["max_snaps"]:]

    # ---------- scoring ----------
    def rate_between(self, a, b):
        """likes/hour between snapshots a (older) and b (newer); comment-proxy fallback."""
        dh = (parse_iso(b["t"]) - parse_iso(a["t"])).total_seconds() / 3600
        if dh < self.cfg["min_snap_gap_hours"]:
            return None
        if a["likes"] is not None and b["likes"] is not None:
            return max(0.0, (b["likes"] - a["likes"]) / dh)
        if a["comments"] is not None and b["comments"] is not None:
            return max(0.0, (b["comments"] - a["comments"]) / dh * self.cfg["comments_to_likes"])
        return None

    def score(self, rec):
        snaps = rec["snaps"]
        if len(snaps) < 2:
            return None
        rate = self.rate_between(snaps[0] if len(snaps) < 3 else snaps[-3], snaps[-1])
        if rate is None:
            return None
        accel = None
        if len(snaps) >= 3:
            # trailing interval vs the one before it — same-width windows, so
            # the signal means the same thing whether we have 3 snaps or 12
            r_prev = self.rate_between(snaps[-3], snaps[-2])
            r_now = self.rate_between(snaps[-2], snaps[-1])
            if r_prev and r_now and r_prev > 0:
                accel = r_now / r_prev
        return {"rate": round(rate, 1), "accel": round(accel, 2) if accel else None}

    def should_fire(self, rec, sc):
        cfg = self.cfg
        if rec["fired"] or rec.get("dl_fails", 0) >= cfg["max_dl_fails"]:
            return False
        # freshness: beyond the paging horizon a reel's snaps stop updating,
        # so its score freezes at the last sighting — never fire on that
        if now() - parse_iso(rec["snaps"][-1]["t"]) > timedelta(hours=cfg["max_stale_hours"]):
            return False
        if sc["rate"] < cfg["fire_floor_lph"]:
            return False
        if BAIT_RE.search(rec.get("caption", "")):
            return False
        # caption gates (2026-07-29, Marcus): mark once so the log line fires
        # once, not every tick the reel stays hot. Empty captions pass — the
        # architect's permalink-fetch fallback still covers those.
        if rec.get("skip_reason"):
            return False
        cap = rec.get("caption", "")
        if cap:
            skip = None
            if (len(NON_LATIN_RE.findall(cap)) >= 3
                    or len({w.lower() for w in NON_EN_WORDS_RE.findall(cap)}) >= 2):
                skip = "non-English caption"
            elif GEAR_RE.search(cap):
                skip = "gear-flex caption"
            if skip:
                rec["skip_reason"] = skip
                log(f"  SKIP ({skip}) #{rec['tag']} {rec['permalink']}")
                return False
        # comment-farm signature (architect, job 20260729-022123: 169 comments
        # vs 60 likes) — comment-gated bait spikes on comment velocity; a
        # genuinely reaching reel never has comments outnumber real likes
        s_last = rec["snaps"][-1]
        if (s_last.get("likes") or 0) >= 20 and (s_last.get("comments") or 0) > s_last["likes"]:
            return False
        # Acceleration is MANDATORY (audit finding #3): a mature viral cruises
        # at a huge rate with accel ~1.0; a genuine spike is speeding up. This
        # also means nothing fires off the 2-snapshot catch-up delta — a media
        # needs >=3 snapshots (~1h of history) before it can fire.
        # (A former second-opinion gate — in_top OR 2x floor — was removed
        # 2026-07-24 with Marcus: top_media is almost entirely >72h-old posts,
        # so in practice it silently doubled the floor to 600 l/hr.)
        if not sc["accel"] or sc["accel"] < cfg["accel_min"]:
            return False
        # per-tag cooldown
        tag = next((t for t in self.tags["pool"] if t["tag"] == rec["tag"]), None)
        if tag and tag.get("last_fire"):
            if now() - parse_iso(tag["last_fire"]) < timedelta(minutes=cfg["tag_cooldown_min"]):
                return False
        return True

    # ---------- firing ----------
    def probe_clip(self, clip):
        """Cheap ffmpeg triage of a downloaded reference clip.
        Returns (duration_s or None, scene_cuts or None). Any probe failure
        returns Nones — the fire proceeds, the architect stays the judge."""
        ffmpeg = None
        for cand in (Path.home() / ".local" / "bin" / "ffmpeg", Path("/opt/homebrew/bin/ffmpeg"),
                     Path("/usr/local/bin/ffmpeg")):
            if cand.exists():
                ffmpeg = str(cand)
                break
        if not ffmpeg:
            import shutil
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None, None
        try:
            r = subprocess.run([ffmpeg, "-i", str(clip)], capture_output=True,
                               text=True, timeout=30)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
            if not m:
                return None, None
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            return None, None
        if dur > self.cfg["prefilter_max_s"] or dur <= self.cfg["prefilter_scene_min_s"]:
            return dur, None  # cut count not needed for the verdict
        try:
            thresh = self.cfg["prefilter_scene_thresh"]
            r = subprocess.run(
                [ffmpeg, "-i", str(clip), "-vf", f"select='gt(scene,{thresh})',showinfo",
                 "-an", "-f", "null", "-"],
                capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                # decode failure yields 0 showinfo lines, indistinguishable
                # from a genuinely static clip — fail OPEN, let the architect
                # judge (audit 2026-07-28 finding 3)
                return dur, None
            cuts = len(re.findall(r"Parsed_showinfo.*\bn:\s*\d+", r.stderr))
            return dur, cuts
        except Exception:
            return dur, None

    def fire(self, mid, rec, sc):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        clip = CLIPS / f"{stamp}-{mid}.mp4"
        if not rec.get("media_url"):
            rec["dl_fails"] = rec.get("dl_fails", 0) + 1
            log(f"  FIRE ABORT {mid}: no media_url in latest snapshot")
            return
        try:
            subprocess.run(
                ["curl", "-sL", "--max-time", "120", "-o", str(clip), rec["media_url"]],
                check=True, timeout=150)
            if clip.stat().st_size < 50_000:
                raise RuntimeError(f"download too small ({clip.stat().st_size}B) — expired URL?")
        except Exception as e:
            rec["dl_fails"] = rec.get("dl_fails", 0) + 1
            log(f"  FIRE ABORT {mid}: clip download failed ({rec['dl_fails']}/{self.cfg['max_dl_fails']}): {e}")
            clip.unlink(missing_ok=True)
            return

        # pre-fire triage: suppress references the architect has never once
        # accepted, before an opus run is spent on them. Marks the media
        # fired (never re-fires) but does NOT set the tag cooldown — the tag's
        # next genuine spike may still fire this tick.
        dur, cuts = self.probe_clip(clip)
        suppress = None
        if dur and dur > self.cfg["prefilter_max_s"]:
            suppress = f"{dur:.1f}s ref > {self.cfg['prefilter_max_s']}s cap (no ref over 20.7s ever survived triage)"
        elif dur and dur > self.cfg["prefilter_scene_min_s"] and cuts == 0:
            suppress = (f"{dur:.1f}s ref with ZERO scene cuts at "
                        f"{self.cfg['prefilter_scene_thresh']} — no structure to mimic")
        if suppress:
            rec["fired"] = True
            clip.unlink(missing_ok=True)
            log(f"  SUPPRESS {mid} (#{rec['tag']}): {suppress}")
            with open(STATE / "fires.jsonl", "a") as f:
                f.write(json.dumps({"id": f"{stamp}-{mid}", "created": iso(now()),
                                    "tag": rec["tag"], "permalink": rec["permalink"],
                                    "suppressed": suppress}) + "\n")
            return

        job = {
            "id": f"{stamp}-{mid}",
            "created": iso(now()),
            "status": "queued",
            "tag": rec["tag"],
            "permalink": rec["permalink"],
            "posted": rec["posted"],
            "caption": rec["caption"],
            "clip": str(clip),
            "metrics": {
                "likes_per_hour": sc["rate"],
                "accel": sc["accel"],
                "in_top_media": rec.get("in_top", False),
                "latest": rec["snaps"][-1],
            },
            "why": f"#{rec['tag']} reel at {sc['rate']} likes/hr"
                   + (f", accelerating x{sc['accel']}" if sc["accel"] else "")
                   + (", ranked in top_media" if rec.get("in_top") else ""),
        }
        save_json(JOBS / f"{job['id']}.json", job)
        rec["fired"] = True
        tag = next((t for t in self.tags["pool"] if t["tag"] == rec["tag"]), None)
        if tag:
            tag["last_fire"] = iso(now())
            tag["last_spike"] = iso(now())
        self.fired_this_tick += 1
        log(f"  FIRE {job['id']}: {job['why']} -> {clip.name}")
        with open(STATE / "fires.jsonl", "a") as f:
            f.write(json.dumps({k: job[k] for k in ("id", "created", "tag", "permalink", "why")}) + "\n")

    def spawn_queued(self):
        """Spawn the architect for ONE queued job per tick, freshest spike first.
        Architects serialize on a lock anyway — spawning the whole backlog at
        once just makes the losers requeue-churn (audit finding #7). Older
        queued jobs wait their turn; prune_jobs drops them once cold."""
        if in_quiet_window(self.cfg):
            log("  quiet window: leaving queued jobs for a later tick")
            return
        for jf in sorted(JOBS.glob("*.json"), reverse=True):
            job = load_json(jf, None)
            if not job or job.get("status") != "queued":
                continue
            job["status"] = "spawned"
            job["spawned_at"] = iso(now())
            save_json(jf, job)
            with open(LOGS / f"architect-{job['id']}.log", "a") as out:
                subprocess.Popen(
                    [str(ROOT / "run-pulse-architect.sh"), str(jf)],
                    stdout=out, stderr=subprocess.STDOUT,
                    start_new_session=True)
            log(f"  spawned architect for {job['id']}")
            break

    def prune_jobs(self):
        """Age out cold jobs: a spike older than a day is no longer a spike,
        and failed/orphaned jobs must not accumulate in jobs/ forever."""
        for jf in JOBS.glob("*.json"):
            job = load_json(jf, None)
            if not job:
                continue
            ref = job.get("spawned_at") or job.get("created")
            try:
                age_h = (now() - parse_iso(ref)).total_seconds() / 3600
            except (TypeError, ValueError):
                age_h = 999
            if (job.get("status") == "failed" and age_h > 1) or age_h > 24:
                (JOBS / "done").mkdir(exist_ok=True)
                jf.rename(JOBS / "done" / f"{jf.stem}.dropped.json")
                log(f"  job dropped ({job.get('status')}, {round(age_h)}h old): {jf.stem}")

    # ---------- tag pool lifecycle ----------
    def expand_tags(self):
        """Harvest hashtags from near-spike captions into the pool, quota permitting."""
        cutoff_rate = self.cfg["fire_floor_lph"] * self.cfg["near_spike_factor"]
        existing = {t["tag"] for t in self.tags["pool"]}
        counts = {}
        for rec in self.pulse["media"].values():
            sc = self.score(rec)
            if not sc or sc["rate"] < cutoff_rate:
                continue
            # a near-spike also refreshes its tag's liveness
            tag = next((t for t in self.tags["pool"] if t["tag"] == rec["tag"]), None)
            if tag:
                tag["last_spike"] = iso(now())
            for h in HASHTAG_RE.findall(rec.get("caption", "")):
                h = h.lower()
                if h in existing or h in MEGATAGS:
                    continue
                counts[h] = counts.get(h, 0) + 1
        # headroom is computed ONCE and decremented locally per add — the live
        # quota math only sees a new tag after it's in the pool, so consulting
        # it inside the loop would never decrease (audit finding #1)
        headroom = self.quota_headroom()
        adds = 0
        n_active = sum(1 for t in self.tags["pool"] if t.get("active"))
        for h, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if n < self.cfg["tag_add_min_captions"]:
                break  # sorted desc — nothing below has enough independent signal
            if adds >= self.cfg["max_tag_adds_per_tick"]:
                break
            if headroom <= 0 or n_active >= self.cfg["max_pool"]:
                log(f"  expansion halted: headroom={headroom} active={n_active} "
                    f"(candidates left: {len(counts) - adds})")
                break
            self.tags["pool"].append({
                "tag": h, "source": "caption", "added": iso(now()),
                "active": True, "last_spike": None,
            })
            existing.add(h)
            headroom -= 1
            n_active += 1
            adds += 1
            log(f"  TAG+ #{h} (seen in {n} near-spike captions)")

    def evict_tags(self):
        cutoff = now() - timedelta(days=self.cfg["tag_evict_days"])
        active = [t for t in self.tags["pool"] if t.get("active")]
        n_active = len(active)
        for t in active:
            if t["source"] != "caption" or n_active <= self.cfg["min_pool"]:
                continue
            last = t.get("last_spike") or t.get("added")
            if last and parse_iso(last) < cutoff:
                t["active"] = False  # tombstone — still counts for quota until 7d roll off
                n_active -= 1
                log(f"  TAG- #{t['tag']} (no near-spike in {self.cfg['tag_evict_days']}d)")

    def prune_media(self):
        # 2026-07-29: also drop media of deactivated tags — they can never be
        # re-sighted or fire, and #ai alone left 34k dead records (a third of
        # a 166MB pulse.json the tick rewrites every ~30 min)
        cutoff = hhmm_ago(self.cfg["max_age_hours"] + 24)
        active = {t["tag"] for t in self.tags["pool"] if t.get("active")}
        stale = [mid for mid, r in self.pulse["media"].items()
                 if parse_iso(r["posted"]) < cutoff or r["tag"] not in active]
        for mid in stale:
            del self.pulse["media"][mid]

    def activate_pending(self):
        """Curated tags waiting in tags.json ("pending": true) go live one per
        tick as the rolling-7-day hashtag quota frees up (the 07-29 pool purge
        keeps its tombstones charged until ~08-05). Order in the file is
        priority order."""
        n_active = sum(1 for t in self.tags["pool"] if t.get("active"))
        for t in self.tags["pool"]:
            if not t.get("pending"):
                continue
            if self.quota_headroom() <= 0 or n_active >= self.cfg["max_pool"]:
                # the tick-start "quota 7d used" line alone reads as free
                # headroom; say explicitly why pending tags are still waiting
                pending = [p["tag"] for p in self.tags["pool"] if p.get("pending")]
                log(f"  pending tags waiting (headroom {self.quota_headroom()}, "
                    f"active {n_active}): {', '.join(pending)}")
                return
            t["active"] = True
            del t["pending"]
            log(f"  TAG+ #{t['tag']} (curated pending tag, quota freed)")
            return

    # ---------- main ----------
    def tick(self):
        if (ROOT / "PAUSED").exists():
            log("PAUSED file present — tick skipped")
            return
        # empty pool = tags.json failed to load; polling nothing is survivable
        # but prune_media would delete EVERY record and save_json would then
        # overwrite tags.json with the empty pool — refuse the whole tick
        if not self.tags.get("pool"):
            log("tags.json empty or unreadable — aborting tick (refusing to prune)")
            return
        # dry runs must not activate: the tick would poll the new tag (a live
        # quota charge) but never save the activation, leaving charged_7d()
        # blind to it for 7 days
        if not self.dry:
            self.activate_pending()
        active = [t for t in self.tags["pool"] if t.get("active")]
        log(f"tick start: {len(active)} active tags, "
            f"{len(self.pulse['media'])} tracked media, "
            f"quota 7d used={len(self.charged_7d())}/{self.cfg['weekly_quota']}")
        budget = self.cfg["call_budget_per_tick"]
        spent = 0
        # firehose tags deep-sweep EVERY tick: on a tag posting hundreds of
        # reels/hr, the rotation's ~3h return interval can never re-sight a
        # reel 3 times inside the fire gate's ~90-min window
        firehose = [t for t in active if t.get("firehose")]
        normal = [t for t in active if not t.get("firehose")]
        for t in firehose:
            # allowance must fund BOTH chains poll_tag runs on a firehose tag:
            # the captioned mini-chain AND the caption-less deep chain, plus
            # ~2 overhead calls (hid resolve / top_media)
            fh_allow = min(self.cfg["firehose_page_cap"] + self.cfg["firehose_caption_pages"] + 2,
                           budget - spent)
            if fh_allow < 8:
                log(f"  firehose deep: #{t['tag']} SKIPPED (budget left {budget - spent})")
                continue
            log(f"  firehose deep: #{t['tag']} (every tick, allowance {fh_allow}, page cap {self.cfg['firehose_page_cap']})")
            spent += self.poll_tag(t, fh_allow, deep=True)
            t["last_deep"] = iso(now())
        # rotating deep sweep among the rest: the tag longest without a long
        # scroll gets paged toward 24h this tick
        deep_tag = min(normal, key=lambda t: t.get("last_deep") or "") if normal else None
        if deep_tag is not None:
            deep_allow = min(self.cfg["deep_page_cap"] + 2, budget - spent)  # +2: hid resolve + top_media
            log(f"  deep sweep: #{deep_tag['tag']} (toward {self.cfg['deep_horizon_hours']}h)")
            spent += self.poll_tag(deep_tag, deep_allow, deep=True)
            deep_tag["last_deep"] = iso(now())
        # fresh sweep for the rest, least-recently-polled first so a budget
        # truncation never starves the same tags every tick
        fresh = [t for t in normal if t is not deep_tag]
        fresh.sort(key=lambda t: t.get("last_polled") or "")
        allowance = max(4, (budget - spent) // max(1, len(fresh)))
        for t in fresh:
            spent += self.poll_tag(t, min(allowance, budget - spent))
            if spent >= budget:
                log(f"  call budget ({budget}) exhausted — remaining tags wait for next tick")
                break
        log(f"  api calls this tick: {spent}/{budget}")

        scored = []
        for mid, rec in self.pulse["media"].items():
            sc = self.score(rec)
            if sc:
                scored.append((sc["rate"], mid, rec, sc))
        scored.sort(reverse=True, key=lambda x: x[0])
        for rate, mid, rec, sc in scored[:5]:
            age_h = (now() - parse_iso(rec["snaps"][-1]["t"])).total_seconds() / 3600
            stale = f" [stale {age_h:.1f}h]" if age_h > self.cfg["max_stale_hours"] else ""
            log(f"  top: {rate} l/hr {'*fired*' if rec['fired'] else ''}"
                f"#{rec['tag']} {rec['permalink']} accel={sc['accel']} top={rec.get('in_top')}{stale}")

        if not self.dry:
            for rate, mid, rec, sc in scored:
                if self.should_fire(rec, sc):
                    self.fire(mid, rec, sc)
            self.expand_tags()
            self.evict_tags()
            self.prune_media()
            self.prune_jobs()
            save_json(STATE / "tags.json", self.tags)
            save_json(STATE / "pulse.json", self.pulse)
            self.spawn_queued()
        log(f"tick done: fired {self.fired_this_tick}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    # audit 2026-07-28: a direct invocation (dry runs included) used to bypass
    # run-pulse-tick.sh's tick.lock and run concurrently with a live tick —
    # two ticks in one clock hour breached Meta's ~200 calls/hr ceiling. The
    # wrapper holds the lock already and says so via PULSE_LOCK_HELD; anyone
    # else must take it here or leave.
    _lock = STATE / "tick.lock"
    _self_lock = os.environ.get("PULSE_LOCK_HELD") != "1"
    if _self_lock:
        try:
            if _lock.exists() and time.time() - _lock.stat().st_mtime > 7200:
                _lock.rmdir()  # stale (>2h) lock from a crashed tick
        except OSError:
            pass
        try:
            _lock.mkdir()
        except OSError:
            print("tick.lock held — a tick is already running; refusing to start "
                  "(concurrent ticks double the Graph call rate). Retry in a few minutes.")
            sys.exit(1)
    try:
        Monitor(dry_run=dry).tick()
    finally:
        if _self_lock:
            try:
                _lock.rmdir()
            except OSError:
                pass
