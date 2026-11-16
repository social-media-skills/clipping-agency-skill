#!/usr/bin/env python3
"""End-to-end test: two made-up clients go through every script, with good and broken input.

    python3 tests/run_pipeline.py              # needs Python 3.9+ and ffmpeg; Pillow to test cutting
    python3 tests/run_pipeline.py --no-cut     # without Pillow: everything except make_clips.py

Builds a test episode with ffmpeg, then: client.py (checklist, bad fields, the account and project guard,
connect, saved accounts), sources.py (folder, feed, links, the seen-list), transcribe.py --from,
make_clips.py (brand, names dictionary, refusals), check_clips.py (every rule, the same file for two
clients, the same file twice for one client), approve.py (the page without scripts, the plain-text
reply in any encoding, an older page, a repeated reply, the wrong approver, a re-edit voiding an
approval, a rule changed after sending, approval on silence by an absolute deadline, a skip on a
scheduled clip), schedule.py (only approved clips, spacing, planned slots and time zones, TikTok's
choices, the account guard, an edited request, --check right before sending, --posted, --cancelled,
--status), status.py, report.py (statuses refreshed from analytics, the math, agency notes apart),
offboarding and releasing an account, upload_parts.py without a network, and wrong input everywhere: a message, never a
traceback. Nothing touches the network and nothing is posted.
"""
import copy, hashlib, json, os, re, shutil, subprocess, sys, tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "clipping-agency-skill")
S = os.path.join(SKILL, "scripts")
PY = sys.executable
NO_CUT = "--no-cut" in sys.argv
try:
    import PIL  # noqa: F401  (make_clips.py draws with Pillow; without it, test everything else)
except ImportError:
    if not NO_CUT:
        print("Pillow isn't installed for this Python, so make_clips.py is skipped (same as --no-cut)")
    NO_CUT = True
sys.dont_write_bytecode = True
sys.path.insert(0, S)
import agencylib as lib  # noqa: E402

failed = []
checks = 0
HOME = [""]
T0 = "2026-11-02T09:00:00Z"   # a Monday, the day after the US clocks went back


def fail(msg):
    failed.append(msg)
    print("FAIL", msg)


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        fail(msg)


def run(script, *args, cwd, expect=0, now=T0):
    env = dict(os.environ)
    for k in ("AGENCY_DEBUG", "CLIPPING_AGENCY_LEDGER"):
        env.pop(k, None)
    env["CLIPPING_AGENCY_HOME"] = HOME[0]
    env["AGENCY_NOW"] = now
    r = subprocess.run([PY, os.path.join(S, script)] + [str(a) for a in args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=cwd, env=env)
    out = r.stdout + r.stderr
    what = f"{script} {' '.join(str(a) for a in args)}"
    ok("Traceback" not in out, f"{what} crashed with a traceback:\n{out[-1500:]}")
    if expect is not None:
        ok(r.returncode == expect, f"{what} exited {r.returncode}, expected {expect}:\n{out[-1500:]}")
    return r.returncode, out


def refused(script, *args, cwd, needle=None, now=T0):
    """A run that must stop with exit 1 and a clear line starting with 'error:', no Python error names."""
    code, out = run(script, *args, cwd=cwd, expect=1, now=now)
    what = f"{script} {' '.join(str(a) for a in args)}"
    ok("error" in out.lower(), f"{what} should say error:\n{out[-800:]}")
    ok(not re.search(r"\b[A-Z]\w+(Error|Exception)\b|Errno", out), f"{what} shows a Python error name:\n{out[-800:]}")
    if needle:
        ok(needle.lower() in out.lower(), f"{what}: expected '{needle}' in:\n{out[-1200:]}")
    return out


def has(out, needle, what):
    ok(needle.lower() in out.lower(), f"{what}: expected '{needle}' in:\n{out[-1500:]}")


def ffmpeg(*args, cwd):
    r = subprocess.run(["ffmpeg", "-y", "-v", "error"] + list(args), capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        sys.exit("could not build a test video; is ffmpeg installed with libx264?")


def wjson(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def rjson(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


SENTENCES = {
    1: "Welcome back to the test show with Jon Smyth.",
    8: "Here is the first idea that stands on its own.",
    15: "And here is a second idea that people will share.",
    29: "This episode is brought to you by Acme, use the code TEST.",
    50: "We never talk about politics on this show.",
}


def make_source(d):
    """A 60 s horizontal test episode with a tone, and captions: a sentence every 2 s, a [MUSIC] cue at 40 s."""
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=60", "-f", "lavfi", "-i",
           "sine=frequency=440:duration=60", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", "source.mp4", cwd=d)
    stamp = lambda t: f"00:00:{int(t):02d},{int(round((t % 1) * 1000)):03d}"  # noqa: E731
    cues, n = [], 0
    for i in range(30):
        a, b = i * 2.0, i * 2.0 + 1.8
        n += 1
        if 40 <= a < 44:
            text = "[MUSIC]"
        else:
            text = SENTENCES.get(int(a), SENTENCES.get(int(a) - 1, f"This is test sentence number {i + 1}."))
        cues.append(f"{n}\n{stamp(a)} --> {stamp(b)}\n>> {text}\n")
    with open(os.path.join(d, "source.srt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(cues))


def plain_clip(src, out, start, length):
    """A vertical clip cut straight from the source with ffmpeg: a clip the agency made in its own editor."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    ffmpeg("-ss", str(start), "-t", str(length), "-i", src, "-vf", "crop=405:720,scale=540:960,setsar=1",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000",
           "-movflags", "+faststart", out, cwd=os.path.dirname(out))


def client(cid, name, **over):
    c = {"id": cid, "name": name, "status": "active", "example": False, "show": name, "sources": [],
         "platforms": ["TIKTOK", "INSTAGRAM", "YOUTUBE"], "clips_per_episode": 3, "agreed_clips_per_month": 6,
         "cadence": {"days": ["mon", "wed", "fri"], "times": ["09:00"], "max_per_day": 1, "min_gap_hours": 3,
                     "start_after_hours": 2},
         "timezone": "America/New_York",
         "brand": {"accent": "#FFD43B", "text": "#FFFFFF", "hook_bg": "#FFFFFF", "hook_text": "#000000",
                   "uppercase": False, "font": None, "logo": None, "logo_position": "top-right",
                   "end_card": {"text": f"Full episode: {name}", "seconds": 1.0, "bg": "#101820", "color": "#FFFFFF"}},
         "names": {"John Smith": ["Jon Smyth", "Jon Smith"]}, "no_go": ["politics"],
         "approvers": [{"name": "Pat Test", "contact": "pat@example.com"}],
         "approval": {"rule": "hold"}, "guest_release": "per_episode",
         "youtube": {"privacy": "public", "madeForKids": False},
         "woopsocial": {"projectId": f"proj-{cid}", "accounts": {
             "TIKTOK": {"socialAccountId": f"acc-{cid}-tt", "handle": f"@{cid}",
                        "privacyOptions": ["PUBLIC_TO_EVERYONE", "FOLLOWER_OF_CREATOR", "SELF_ONLY"]},
             "INSTAGRAM": {"socialAccountId": f"acc-{cid}-ig", "handle": f"@{cid}"},
             "YOUTUBE": {"socialAccountId": f"acc-{cid}-yt", "handle": f"@{cid}"}}},
         "agency": {"name": "Test Agency", "reply_to": "agency@example.com"},
         "report": {"recipients": []}, "expect_episode_every_days": 7}
    c.update(over)
    return c


def caps(text, sponsor=False):
    t = ("#ad " if sponsor else "") + text
    return {"TIKTOK": t + " #test", "INSTAGRAM": t + "\n#test", "YOUTUBE": t + " #Shorts"}


def clips_spec(own=False):
    base = [
        {"id": "01", "start": 8.0, "end": 14.0, "why": "the first idea stands alone and opens on the point",
         "hook": "The first idea", "title": "The first idea", "captions": caps("The first idea, explained.")},
        {"id": "02", "start": 15.0, "end": 21.0, "why": "a second idea people will share with a friend",
         "hook": "The second idea", "title": "The second idea", "captions": caps("The second idea, in 6 seconds.")},
        {"id": "03", "start": 28.0, "end": 34.0, "why": "the sponsor read, kept on purpose for the sponsor",
         "hook": "A word from Acme", "title": "A word from Acme", "captions": caps("Sponsored by Acme.", True)},
    ]
    if own:
        for c in base:
            c["file"] = f"own/clip-{c['id']}.mp4"
    return base


def body_sha(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---- unit tests of the shared helpers

def unit_tests():
    from datetime import datetime, timedelta, timezone
    # the report names a thin month of episodes as the cause of a shortfall
    import report as rep
    base = {"later": [], "waiting": 0, "pending_change": 0, "skipped": 0, "failed": [],
            "a": datetime(2026, 10, 1, tzinfo=timezone.utc), "b": datetime(2026, 11, 1, tzinfo=timezone.utc)}
    why = rep.shortfall(dict(base, eps_month=1, c={"expect_episode_every_days": 14}), 3, 6)
    ok(any("1 new episode reached us this month, where 2 were expected" in w for w in why), f"thin month: {why}")
    why = rep.shortfall(dict(base, eps_month=0, c={"expect_episode_every_days": 14}), 0, 6)
    ok(why == ["no new episode reached us this month"], f"no episode: {why}")
    why = rep.shortfall(dict(base, eps_month=1, c={}), 3, 6)
    ok("isn't in our records" in why[0], f"unknown cause said plainly: {why}")
    c = {"timezone": "America/New_York", "cadence": {"days": ["mon", "wed", "fri"], "times": ["09:00"],
                                                     "max_per_day": 1, "min_gap_hours": 3, "start_after_hours": 2}}
    now = lib.parse_time(T0)
    slots = lib.plan_slots(c, 3, now=now)
    ok([lib.iso(t) for t in slots] == ["2026-11-02T14:00:00Z", "2026-11-04T14:00:00Z", "2026-11-06T14:00:00Z"],
       f"plan_slots after the clocks went back (EST): {[lib.iso(t) for t in slots]}")
    # before the change the same clock time is an hour earlier in UTC
    early = lib.plan_slots(c, 1, now=lib.parse_time("2026-10-26T09:00:00Z"))
    ok(lib.iso(early[0]) == "2026-10-26T13:00:00Z", f"plan_slots in EDT: {lib.iso(early[0])}")
    # a time that doesn't exist on the night the clocks go forward moves an hour on
    c2 = {"timezone": "America/New_York", "cadence": {"days": ["sun"], "times": ["02:30"], "max_per_day": 1}}
    dst = lib.plan_slots(c2, 1, now=lib.parse_time("2027-03-13T12:00:00Z"))
    ok(lib.clock(dst[0], "America/New_York").startswith("Sun 14 Mar 03:30"), f"nonexistent time: {lib.clock(dst[0], 'America/New_York')}")
    # Amsterdam keeps its own clock
    c3 = {"timezone": "Europe/Amsterdam", "cadence": {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                                      "times": ["12:00"], "max_per_day": 1}}
    ams = lib.plan_slots(c3, 2, now=now)
    ok([lib.iso(t) for t in ams] == ["2026-11-02T11:00:00Z", "2026-11-03T11:00:00Z"], f"Amsterdam: {[lib.iso(t) for t in ams]}")
    # spacing: never closer than min_gap_hours to a post already scheduled; at most max_per_day
    c4 = {"timezone": "UTC", "cadence": {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                         "times": ["09:00", "10:00", "15:00"], "max_per_day": 2, "min_gap_hours": 3,
                                         "start_after_hours": 0}}
    busy = [lib.parse_time("2026-11-02T08:30:00Z")]
    sp = lib.plan_slots(c4, 3, busy, now=lib.parse_time("2026-11-02T00:00:00Z"))
    ok([lib.iso(t) for t in sp] == ["2026-11-02T15:00:00Z", "2026-11-03T09:00:00Z", "2026-11-03T15:00:00Z"],
       f"spacing and per-day: {[lib.iso(t) for t in sp]}")
    for a, b in zip(sp, sp[1:]):
        ok(b - a >= timedelta(hours=3), "slots closer than min_gap_hours")
    # TikTok's daily cap: a day whose TikTok account already has 15 posts is skipped
    tt = [lib.parse_time("2026-11-02T00:00:00Z") + timedelta(minutes=i) for i in range(lib.TIKTOK_DAILY_CAP)]
    capd = lib.plan_slots(c4, 1, (), tt, now=lib.parse_time("2026-11-02T00:00:00Z"))
    ok(lib.iso(capd[0]).startswith("2026-11-03"), f"TikTok daily cap: {lib.iso(capd[0])}")
    ok(lib.cadence_of({"cadence": {"max_per_day": 99}})["max_per_day"] == lib.TIKTOK_DAILY_CAP, "max_per_day capped")
    # not before the episode is out
    nb = lib.plan_slots(c, 1, now=now, not_before=lib.parse_time("2026-11-05T00:00:00Z"))
    ok(lib.iso(nb[0]) == "2026-11-06T14:00:00Z", f"not_before: {lib.iso(nb[0])}")
    # the plain-text reply
    import approve
    found, probs = approve.parse_reply("1 yes, 2 change: start later, 3 skip", 3)
    ok(found == {1: ("approve", ""), 2: ("revise", "start later"), 3: ("reject", "")} and not probs,
       f"one-line reply: {found} {probs}")
    found, probs = approve.parse_reply("Clip 1: yes\nclip 2 - skip\n3) yes", 3)
    ok(found == {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}, f"reply on lines: {found}")
    found, _ = approve.parse_reply("All yes, thanks!", 2)
    ok(found == {1: ("approve", ""), 2: ("approve", "")}, f"all yes: {found}")
    ok(approve.parse_reply("1: change", 1)[1], "a change with no note is a problem")
    reply_parser_tests(approve)
    tk, prob = approve.parse_tiktok("TikTok: friends, no comments, duets on, stitches off")
    ok(tk == {"privacyLevel": "MUTUAL_FOLLOW_FRIENDS", "allowComment": False, "allowDuet": True, "allowStitch": False}
       and not prob, f"TikTok line: {tk} {prob}")
    tk, prob = approve.parse_tiktok("TikTok: everyone")
    ok(tk is None and "comments" in prob, "an incomplete TikTok line chooses nothing")
    # clean edges: what a transcriber heard at the edges against the planned words
    import check_clips
    ok(check_clips.edge_problems(["Here", "is"], ["will", "share."], ["Here", "is", "the"], ["people", "will", "share"]) == [],
       "clean edges pass")
    probs = check_clips.edge_problems(["first", "idea"], ["share", "And"], ["Here", "is", "the"], ["people", "will", "share"])
    ok(len(probs) == 2 and "cut off" in probs[0] and "next" in probs[1], f"cut-off words flagged: {probs}")
    ok(check_clips.edge_problems(["Here"], ["8", "-1."], ["Here", "is"], ["will", "share"]) == [],
       "noise heard as digits isn't a word")
    import make_clips
    # a cut inside a clip is made only where the sound is quiet (outside the filler removed on purpose)
    lv = {"t0": 0.0, "step": 0.01, "db": [-60.0] * 300}
    for i in range(50, 100):
        lv["db"][i] = -20.0          # the words of the first span
    for i in range(200, 260):
        lv["db"][i] = -20.0          # the words of the second span
    sp2 = [(0.4, 1.1), (1.9, 2.7)]
    ok(make_clips.guard_cuts(sp2, [], lv) == (sp2, 0), "a quiet pause is cut")
    lv2 = dict(lv, db=list(lv["db"]))
    for i in range(140, 160):
        lv2["db"][i] = -22.0         # a word the transcript timed wrong, inside the pause
    ok(make_clips.guard_cuts(sp2, [], lv2) == ([(0.4, 2.7)], 1), "a pause with a word in it is never cut")
    filler = [({"start": 1.4, "end": 1.6, "end_heard": 1.6, "w": "um"}, True)]
    ok(make_clips.guard_cuts(sp2, filler, lv2)[1] == 0, "the filler removed on purpose doesn't stop its cut")
    ok(make_clips.keep_spans([({"start": 1.0, "end": 1.4}, False), ({"start": 2.5, "end": 2.9}, False)], 1.0, 3.0,
                             False) == [(0.88, 3.1)], "tighten off: one span, no cut inside the clip")
    tx = make_clips.track_x({"track": [[10.0, 0.5], [12.0, 0.25]]}, 10.0, 180, 640)
    ok(tx and tx.startswith("if(lt(t,0.000),230.0") and "70.0" in tx, f"track slides the crop: {tx}")
    # the clip's edges follow the sound: a start on a word moves back to the gap, a start in a pause takes some air
    fe = make_clips.fit_edges([(0.55, 2.5)], lv, prev=(0.2, 0.5), nxt=(2.55, 2.62))
    ok(fe[0][1] == 2.5, f"an edge on sound past the middle of the next word stays: {fe}")
    fe1 = make_clips.fit_edges([(0.55, 2.5)], lv, prev=(0.2, 0.45), nxt=(2.45, 3.4))
    ok(fe1[0][0] == 0.55 and 2.6 <= fe1[0][1] < 2.7, f"a last word timed short keeps its tail; a start on sound stays: {fe1}")
    fe2 = make_clips.fit_edges([(0.45, 1.05)], lv)
    ok(0.32 <= fe2[0][0] < 0.45 and 1.05 < fe2[0][1] <= 1.18, f"an edge in a pause takes a little air: {fe2}")
    fe3 = make_clips.fit_edges([(0.45, 1.05)], lv, prev=(0.2, 0.40), nxt=(1.10, 1.5))
    ok(fe3[0][0] >= 0.44 and fe3[0][1] <= 1.06 + 1e-9, f"the air never reaches the words around the clip: {fe3}")
    ok(check_clips.missing_words("till we".split(), "till till we".split()) == [],
       "a stutter make_clips cut isn't a missing word")
    mc = [x["w"] for x in make_clips.mid_caps([{"w": t} for t in "my Emotions were high. No TV for the Earth, I Think so emotions".split()])]
    ok(mc == "my emotions were high. No TV for the Earth, I think so emotions".split(), f"transcriber capitals mid-sentence: {mc}")
    # a clip started at a camera cut inside a word the transcriber timed too early keeps the start and the word
    ww = [{"w": "establish", "start": 39.8, "end": 40.44}, {"w": "now", "start": 40.44, "end": 41.04},
          {"w": "a", "start": 41.04, "end": 41.36}, {"w": "goal.", "start": 41.36, "end": 42.0}]
    mk = make_clips.select_words(ww, 40.70, 42.1, {"id": "02"})
    ok([w["w"] for w, r in mk][:1] == ["now"], f"the word mostly after the start is in the clip: {mk}")
    ok(make_clips.keep_spans(mk, 40.70, 42.1, False, *make_clips.neighbours(ww, 40.70, 42.1))[0][0] == 40.70,
       "a start set inside a mistimed word stays where it was set")
    ok(make_clips.select_words(ww, 40.90, 42.1, {"id": "02"})[0][0]["w"] == "a", "a word mostly before the start isn't")
    ok([w["w"] for w in check_clips.words_in(ww, [(40.70, 42.1)])][:1] == ["now"], "the listening check agrees")
    # the full listen: a word cut out inside the clip is named
    ok(check_clips.missing_words("I don't really think of it so much is a, this is a direction".split(),
                                 "I don't really think of it so much as a place, as a direction.".split())
       and "place" in check_clips.missing_words("so much is a, this is a direction".split(),
                                                "so much as a place, as a direction.".split())[0],
       "a missing word is named")
    ok(check_clips.missing_words("would have a flying and".split(), "would have a unifying and".split()),
       "a word with its start cut off is named")
    ok(check_clips.missing_words("so much is a place".split(), "so much as a place".split()) == [],
       "a short word misheard isn't a missing word")
    marked = [({"start": 1.0, "end": 1.5, "end_heard": 1.5}, False), ({"start": 1.6, "end": 2.0}, False)]
    sp = make_clips.keep_spans(marked, 1.0, 2.0, True, prev_end=0.95, next_start=2.1)
    ok(sp[0][0] >= 0.95 and 2.0 < sp[-1][1] <= 2.1, f"a cut never reaches into the words around it: {sp}")
    # planned slots of other episodes count; the minimum lead time
    led = {"posts": {}, "planned": {"x|e1|01": {"client": "x", "episode": "e1", "clip": "01",
                                                "scheduledFor": "2026-11-02T14:00:00Z", "accounts": ["tt"]}}}
    busy, acct = lib.client_busy(led, "x", "tt", exclude_episode="e2")
    ok(busy == [lib.parse_time("2026-11-02T14:00:00Z")] and acct == busy, "another episode's planned slot is busy")
    ok(lib.client_busy(led, "x", "tt", exclude_episode="e1") == ([], []), "the episode being planned again isn't")
    nb2 = lib.plan_slots(c, 1, busy, now=now)
    ok(lib.iso(nb2[0]) == "2026-11-04T14:00:00Z", f"two episodes never get the same slot: {lib.iso(nb2[0])}")
    ok(lib.cadence_of({"cadence": {"start_after_hours": 0}})["start_after_hours"] >= lib.MIN_LEAD_HOURS,
       "never sooner than the minimum lead time")
    # the one-hour floor in plan_slots itself: a slot 30 minutes away is never planned, whatever the client file says
    c5 = {"timezone": "UTC", "cadence": {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                         "times": ["09:40", "10:30"], "max_per_day": 3, "min_gap_hours": 0,
                                         "start_after_hours": 0}}
    fl = lib.plan_slots(c5, 1, now=lib.parse_time("2026-11-02T09:10:00Z"))
    ok(lib.iso(fl[0]) == "2026-11-02T10:30:00Z", f"plan_slots keeps an hour's lead at least: {lib.iso(fl[0])}")
    # requests are cleared, cancel notes stay
    tmp = tempfile.mkdtemp(prefix="agency-unit-")
    try:
        wjson(os.path.join(tmp, "requests", "index.json"), {"requests": {"clip-01.json": {}}})
        wjson(os.path.join(tmp, "requests", "clip-01.json"), {})
        wjson(os.path.join(tmp, "requests", "cancel-p1.json"), {})
        led2 = {"planned": {"x|e1|01": {"client": "x", "episode": "e1"}, "y|e1|01": {"client": "y", "episode": "e1"}}}
        gone = lib.clear_requests(tmp, led2, "x", "e1")
        ok(gone == ["clip-01.json"] and os.listdir(os.path.join(tmp, "requests")) == ["cancel-p1.json"]
           and list(led2["planned"]) == ["y|e1|01"], f"clear_requests: {gone} {led2}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # names, topics, captions
    fixed, fixes = lib.names_fix("jon smyth met John smith", {"John Smith": ["Jon Smyth"]})
    ok(fixed == "John Smith met John Smith" and len(fixes) == 2, f"names_fix: {fixed} {fixes}")
    ok(lib.wrong_names("Hi Jon Smyth", {"John Smith": ["Jon Smyth"]}) == [("Jon Smyth", "John Smith")], "wrong_names")
    ok(lib.topic_hits("we talk politicians", ["politics"]) == [] and lib.topic_hits("Politics today", ["politics"]),
       "topic_hits")
    h1 = lib.clip_hash("f" * 64, {"captions": {"TIKTOK": "a"}})
    ok(h1 != lib.clip_hash("f" * 64, {"captions": {"TIKTOK": "b"}}), "a caption change changes the clip hash")
    ok(h1 != lib.clip_hash("e" * 64, {"captions": {"TIKTOK": "a"}}), "a file change changes the clip hash")
    ok(h1 == lib.clip_hash("f" * 64, {"captions": {"tiktok": "a"}, "why": "notes don't count"}),
       "the reason and the key case don't change the clip hash")
    ok(lib.text_length("YOUTUBE", "é") == 2, "YouTube counts bytes")


REPLY_TABLE = [
    # (reply, expected answers or None for refused)
    ("All yes but skip 3, the audio is off", None),
    ("All good except clip 2, skip that one", None),
    ("Yes to all apart from number 2", None),
    ("1: yes but change the caption to say Apollo 11\n2: yes\n3: yes", None),
    ("1: yes 2 maybe 3: yes", None),
    ("Please skip 2, rest yes", None),
    ("Clip one: yes\nClip two: skip\nClip three: yes", None),
    ("1: \U0001F44D\n2: \U0001F44D\n3: \U0001F44D", None),
    ("1: yes\n2: yes\nplease skip 3", None),
    ("1: skip, actually yes\n2: yes\n3: yes", None),
    ("1: change: shorter\nand skip 2 please\n2: yes\n3: yes", None),
    ("Page 1: 1 yes, 2 yes, 3 change: shorter", {1: ("approve", ""), 2: ("approve", ""), 3: ("revise", "shorter")}),
    ("Page 2: 1 yes, 3 skip", {1: ("approve", ""), 3: ("reject", "")}),
    ("page 2: 1 yes, 2 change: start later, 3 skip", {1: ("approve", ""), 2: ("revise", "start later"), 3: ("reject", "")}),
    ("1: yes 2: yes 3: skip", {1: ("approve", ""), 2: ("approve", ""), 3: ("reject", "")}),
    ("1) Yes 2) Yes 3) No", {1: ("approve", ""), 2: ("approve", ""), 3: ("reject", "")}),
    ("\u201c1: yes\n2: skip\n3: yes\u201d", {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}),
    ("1: yes\n2: not this one, he mumbles\n3: don't post this please",
     {1: ("approve", ""), 2: ("reject", ""), 3: ("reject", "")}),
    ("1: yes\n2: don\u2019t post\n3: yes", {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}),
    ("1: no changes, perfect\n2: yes\n3: yes", {1: ("approve", ""), 2: ("approve", ""), 3: ("approve", "")}),
    ("1:\u00a0yes\n2:\u00a0skip\n3:\u00a0yes", {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}),
    ("1 \u2013 yes\n2 \u2013 skip\n3 \u2013 yes", {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}),
    ("1\uff1ayes\n2\uff1askip\n3\uff1ayes", {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}),
    ("- 1: yes\n- 2: skip\n\u2022 3: yes", {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}),
    ("**1:** yes\n**2:** skip\n**3:** yes", {1: ("approve", ""), 2: ("reject", ""), 3: ("approve", "")}),
    ("1: yes\n2: change:\nstart on the second sentence\n3: yes",
     {1: ("approve", ""), 2: ("revise", "start on the second sentence"), 3: ("approve", "")}),
    ("1, 2: yes\n3: skip", {1: ("approve", ""), 2: ("approve", ""), 3: ("reject", "")}),
    ("All yes, thanks!", {1: ("approve", ""), 2: ("approve", ""), 3: ("approve", "")}),
    ("Hi,\n1: yes\n2: yes\n3: yes\nThanks so much!\n\nOn Mon, 2 Nov 2026 at 10:00, Agency wrote:\n1: skip",
     {1: ("approve", ""), 2: ("approve", ""), 3: ("approve", "")}),
]

# every reply example printed in the docs, and what it must give (None: refused)
DOC_EXAMPLES = {
    "Page 2, Example Mission Talks ep-007 1: yes 2: change: start on the second sentence 3: skip TikTok: followers, "
    "comments on, duets off, stitches on Name: Jordan Example":
        {1: ("approve", ""), 2: ("revise", "start on the second sentence"), 3: ("reject", "")},
    "page 2: 1 yes, 2 change: start later, 3 skip": {1: ("approve", ""), 2: ("revise", "start later"), 3: ("reject", "")},
    "Page 1 1) Yes 2) Yes 3) No": {1: ("approve", ""), 2: ("approve", ""), 3: ("reject", "")},
    "Page 1, Example Mission Talks ep-007 1 yes, 2 yes 3: change: lead with the TV, it's funnier than the complaint "
    "TikTok: followers, comments on, duets off, stitches on Name: Jordan Example":
        {1: ("approve", ""), 2: ("approve", ""), 3: ("revise", "lead with the TV, it's funnier than the complaint")},
    "Page 2: 1 yes, 3 skip": {1: ("approve", ""), 3: ("reject", "")},
    "Page 1: 1 yes, 2 yes, 3 change: shorter": {1: ("approve", ""), 2: ("approve", ""), 3: ("revise", "shorter")},
    "All yes but skip 3": None, "Yes to all apart from number 2": None, "1: yes but change the caption": None,
    "2 maybe": None,
}


def doc_reply_examples():
    """Every reply example printed in README.md, SKILL.md and references/approval.md."""
    out = []
    for f in (os.path.join(ROOT, "README.md"), os.path.join(SKILL, "SKILL.md"),
              os.path.join(SKILL, "references", "approval.md")):
        text = open(f, encoding="utf-8").read()
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.S):
            if re.match(r"(?i)\s*page\s*\d+", block):
                out.append((os.path.basename(f), block))
        for q in re.findall(r'"((?:Page|page)\s*\d+:[^"]+)"', text):
            out.append((os.path.basename(f), q))
        for para in re.findall(r"Refused, for example:(.*?)\n- ", text, flags=re.S):
            out += [(os.path.basename(f), q) for q in re.findall(r"`([^`]+)`", para)]
    return out


def reply_parser_tests(approve):
    for text, want in REPLY_TABLE:
        found, probs = approve.parse_reply(text, 3)
        if want is None:
            ok(bool(probs), f"refused, nothing recorded: {text!r} gave {found}")
        else:
            ok(not probs and found == want, f"read as meant: {text!r} gave {found} {probs}")
    seen = doc_reply_examples()
    ok(len(seen) >= 8, f"the docs' reply examples were found: {len(seen)}")
    for where, ex in seen:
        key = " ".join(ex.split())
        ok(key in DOC_EXAMPLES, f"{where}: a reply example with no expected result in this test: {key!r}")
        if key not in DOC_EXAMPLES:
            continue
        found, probs = approve.parse_reply(ex, 3, drop=("Example Mission Talks",))
        want = DOC_EXAMPLES[key]
        if want is None:
            ok(bool(probs), f"{where}: the docs say this is refused: {key!r} gave {found}")
        else:
            ok(not probs and found == want, f"{where}: the docs' example reads as meant: {key!r} gave {found} {probs}")


# ---- the agency folder

def setup_clients(ws, d):
    refused("client.py", "new", "Bad_Id", cwd=ws, needle="can't be a client id")
    run("client.py", "new", "alpha", "--name", "Alpha Show", cwd=ws)
    refused("client.py", "new", "alpha", cwd=ws, needle="already exists")
    out = refused("client.py", "check", "alpha", cwd=ws, needle="timezone")
    has(out, "madeForKids", "template check")
    ok(os.path.isfile(os.path.join(ws, "clients", "alpha", "checklist.md")), "checklist.md written")
    cpath = os.path.join(ws, "clients", "alpha", "client.json")
    wjson(cpath, client("alpha", "Alpha Show"))
    _, out = run("client.py", "check", "alpha", cwd=ws)
    has(out, "0 error(s)", "good client")
    # bad fields, one at a time
    for change, needle in [
        (lambda c: c.update(timezone="Mars/Olympus"), "isn't a time zone"),
        (lambda c: c["cadence"].update(times=["9am"]), "cadence.times"),
        (lambda c: c["cadence"].update(max_per_day=20), "TikTok's daily cap"),
        (lambda c: c.update(approval={"rule": "auto_after_hours", "hours": 24}), "written agreement"),
        (lambda c: c.update(approval={"rule": "post_anyway"}), "approval.rule"),
        (lambda c: c.update(approvers=[]), "who approves"),
        (lambda c: c["brand"].update(accent="yellow"), "brand.accent"),
        (lambda c: c["brand"].update(logo="nothere.png"), "isn't there"),
        (lambda c: c.update(names={"John Smith": "Jon"}), "names dictionary"),
        (lambda c: c.update(youtube={"privacy": "public"}), "never guess"),
        (lambda c: c.update(guest_release="covered_by_contract"), "guest_release_note"),
        (lambda c: c.update(platforms=["TIKTOK", "FAX"]), "platforms"),
        (lambda c: c.update(clips_per_episode=40), "clips_per_episode"),
        (lambda c: c["cadence"].update(start_after_hours=0.5), "at least an hour"),
    ]:
        bad = client("alpha", "Alpha Show")
        change(bad)
        wjson(cpath, bad)
        refused("client.py", "check", "alpha", cwd=ws, needle=needle)
    wjson(cpath, client("alpha", "Alpha Show"))
    # the second client; the guard on accounts and projects
    run("client.py", "new", "beta", "--name", "Beta Talks", cwd=ws)
    bpath = os.path.join(ws, "clients", "beta", "client.json")
    beta = client("beta", "Beta Talks", timezone="Europe/Amsterdam", platforms=["TIKTOK", "YOUTUBE"],
                  cadence={"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "times": ["14:00"],
                           "max_per_day": 1, "min_gap_hours": 3, "start_after_hours": 1},
                  approval={"rule": "auto_after_hours", "hours": 24, "agreed_in_writing": "email from Pat, 1 Nov 2026"},
                  woopsocial={"projectId": None, "accounts": {}})
    wjson(bpath, beta)
    _, out = run("client.py", "connect", "beta", cwd=ws)
    has(out, "projects_create", "connect without a project")
    has(out, "oauth_create_authorization", "connect links")
    has(out, "never need your password", "the client message")
    has(out, "allow WoopSocial", "the client message names the service on the permission screen")
    has(out, "Connect Beta Talks' own accounts", "the possessive of a name ending in s")
    has(out, "2 social accounts", "connect says the free plan")
    refused("client.py", "project", "beta", "proj-alpha", cwd=ws, needle="client alpha's")
    run("client.py", "project", "beta", "proj-beta", cwd=ws)
    # the saved social_accounts_list reply, with an account of another project mixed in
    wjson(os.path.join(d, "accounts.json"), {"content": [{"type": "text", "text": json.dumps({"socialAccounts": [
        {"id": "acc-beta-tt", "platform": "TIKTOK", "username": "@beta", "status": "CONNECTED", "projectId": "proj-beta"},
        {"id": "acc-beta-yt", "platform": "YOUTUBE", "username": "@beta", "status": "CONNECTED", "projectId": "proj-beta"},
        {"id": "acc-alpha-tt", "platform": "TIKTOK", "username": "@alpha", "status": "CONNECTED", "projectId": "proj-alpha"}]})}]})
    wjson(os.path.join(d, "tt-inputs.json"), {"privacyLevelOptions": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                                              "maxVideoPostDurationSec": 600})
    _, out = run("client.py", "accounts", "beta", os.path.join(d, "accounts.json"), os.path.join(d, "tt-inputs.json"), cwd=ws)
    b = rjson(bpath)
    ok(b["woopsocial"]["accounts"]["TIKTOK"]["socialAccountId"] == "acc-beta-tt", "accounts from the saved reply")
    ok(b["woopsocial"]["accounts"]["TIKTOK"].get("privacyOptions") == ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
       "TikTok audiences from the platform inputs")
    ok(lib.load_ledger(os.path.join(HOME[0], "ledger.json"))["accounts"].get("acc-beta-tt") == "beta",
       "the ledger records whose account it is")
    # an account of alpha's put in beta's file is refused, both ways
    b["woopsocial"]["accounts"]["INSTAGRAM"] = {"socialAccountId": "acc-alpha-ig"}
    b["platforms"] = ["TIKTOK", "YOUTUBE", "INSTAGRAM"]
    wjson(bpath, b)
    refused("client.py", "check", "beta", cwd=ws, needle="alpha's file too")
    refused("client.py", "check", "alpha", cwd=ws, needle="beta's file too")
    del b["woopsocial"]["accounts"]["INSTAGRAM"]
    b["platforms"] = ["TIKTOK", "YOUTUBE"]
    wjson(bpath, b)
    run("client.py", "check", "beta", cwd=ws)
    _, out = run("client.py", "list", cwd=ws)
    has(out, "alpha", "list")
    has(out, "2/2 accounts connected", "list counts accounts")


def source_tests(ws, d):
    refused("sources.py", "check", "nobody", cwd=ws, needle="no client")
    folder = os.path.join(d, "synced")
    os.makedirs(folder)
    shutil.copy(os.path.join(d, "source.mp4"), os.path.join(folder, "episode-41.mp4"))
    feed = os.path.join(d, "feed.xml")
    with open(feed, "w", encoding="utf-8") as fh:
        fh.write("""<?xml version="1.0"?><rss><channel><title>Beta</title>
<item><title>Episode 9</title><guid>ep9</guid><pubDate>Sun, 01 Nov 2026 10:00:00 +0000</pubDate>
<enclosure url="https://example.com/ep9.mp4" type="video/mp4" length="1000"/></item>
<item><title>Episode 8</title><guid>ep8</guid><pubDate>Sun, 25 Oct 2026 10:00:00 +0000</pubDate>
<enclosure url="https://example.com/ep8.mp3" type="audio/mpeg" length="1000"/></item>
<item><title>Not media</title><guid>x</guid><enclosure url="https://example.com/x.pdf" type="application/pdf"/></item>
</channel></rss>""")
    a = rjson(os.path.join(ws, "clients", "alpha", "client.json"))
    a["sources"] = [{"type": "folder", "path": folder}]
    wjson(os.path.join(ws, "clients", "alpha", "client.json"), a)
    b = rjson(os.path.join(ws, "clients", "beta", "client.json"))
    b["sources"] = [{"type": "rss", "url": "file://" + feed.replace(os.sep, "/") if os.name != "nt" else feed}]
    wjson(os.path.join(ws, "clients", "beta", "client.json"), b)
    _, out = run("sources.py", "check", "--all", cwd=ws)
    has(out, "episode-41", "folder source")
    has(out, "Episode 9", "feed source")
    ok("Episode 8" not in out, "a first look at a feed takes only the newest episode:\n" + out)
    _, out = run("sources.py", "check", "--all", cwd=ws)
    ok(out.count("nothing new") == 2, "the seen-list: nothing new the second time:\n" + out)
    _, out = run("sources.py", "add", "beta", "https://www.youtube.com/watch?v=abc", cwd=ws)
    has(out, "doesn't download it", "a YouTube link is queued, never downloaded")
    refused("sources.py", "add", "beta", "https://www.youtube.com/watch?v=abc", cwd=ws, needle="already")
    refused("sources.py", "start", "alpha", "q999", cwd=ws, needle="no queue item")
    run("sources.py", "start", "alpha", "q001", "--episode", "ep-41", cwd=ws)
    refused("sources.py", "start", "alpha", "q001", cwd=ws, needle="already started")
    bp = os.path.join(ws, "clients", "alpha", "episodes", "ep-41", "batch.json")
    ok(os.path.isfile(bp), "start writes batch.json")
    ok(rjson(bp)["episode"]["source"] == os.path.join(folder, "episode-41.mp4"), "the source is the synced file")
    return bp


def build_batch(ws, d, cid, ep, own, published="2026-11-01T12:00:00Z", spec=None):
    edir = os.path.join(ws, "clients", cid, "episodes", ep)
    os.makedirs(edir, exist_ok=True)
    shutil.copy(os.path.join(d, "source.mp4"), os.path.join(edir, "source.mp4"))
    batch = {"client": cid, "episode": {"id": ep, "title": f"{cid} {ep}", "published": published, "source": "source.mp4",
                                        "transcript": "work/transcript.json", "guests": ["Sam Guest"],
                                        "guest_release": "on_file", "sponsor_segments": [{"start": 29.0, "end": 33.0,
                                                                                          "sponsor": "Acme"}],
                                        "music": []},
             "clips": spec if spec is not None else clips_spec(own)}
    bp = os.path.join(edir, "batch.json")
    wjson(bp, batch)
    _, out = run("transcribe.py", "source.mp4", "--from", os.path.join(d, "source.srt"), "--out", "work", cwd=edir)
    tr = rjson(os.path.join(edir, "work", "transcript.json"))
    ok(any("MUSIC" in str(e.get("text")) for e in tr.get("events", [])), "the [MUSIC] cue is kept as an event")
    ok(not any(w["w"].startswith(">>") or "MUSIC" in w["w"] for w in tr["words"]), "cues and >> aren't words")
    ok(len(tr.get("levels", [])) >= 50, "levels per second")
    if own:
        for c in batch["clips"]:
            if c.get("file"):
                plain_clip(os.path.join(edir, "source.mp4"), os.path.join(edir, c["file"]), c["start"], c["end"] - c["start"])
    return bp, edir


def cut_tests(ws, bp, edir):
    """make_clips.py with the client's brand. Returns True when the clips were cut by it."""
    b = rjson(bp)
    bad = copy.deepcopy(b)
    bad["clips"][0]["music"] = "trending.mp3"
    wjson(bp, bad)
    refused("make_clips.py", bp, "--width", "360", cwd=ws, needle="never adds music")
    bad = copy.deepcopy(b)
    bad["clips"][0]["speed"] = 1.2
    wjson(bp, bad)
    refused("make_clips.py", bp, cwd=ws, needle="never changes speed")
    bad = copy.deepcopy(b)
    bad["client"] = "beta"
    wjson(bp, bad)
    refused("make_clips.py", bp, cwd=ws, needle="another client's folder")
    wjson(bp, b)
    refused("make_clips.py", bp, "--width", "361", cwd=ws, needle="even number")
    # the brand: a logo drawn here, the end card, the names dictionary on the captions
    cdir = os.path.join(ws, "clients", "alpha")
    from PIL import Image
    Image.new("RGBA", (200, 200), (255, 0, 0, 255)).save(os.path.join(cdir, "logo.png"))
    c = rjson(os.path.join(cdir, "client.json"))
    c["brand"]["logo"] = "logo.png"
    wjson(os.path.join(cdir, "client.json"), c)
    spec = copy.deepcopy(b)
    spec["clips"][0]["start"] = 0.0   # opens on "Welcome back to the test show with Jon Smyth."
    wjson(bp, spec)
    _, out = run("make_clips.py", bp, "--width", "360", cwd=ws)
    man = rjson(os.path.join(edir, "clips", "manifest.json"))
    ok(len(man["clips"]) == 3, "three clips in the manifest")
    ok(man["brand"].get("logo_sha256"), "the manifest records the logo")
    fixed = [f for f in man["clips"][0]["names_fixed"]]
    ok({"from": "Jon Smyth", "to": "John Smith"} in fixed or any(f.get("to") == "John Smith" for f in fixed),
       f"the names dictionary spelled the caption: {fixed}")
    ok(man["clips"][0].get("end_card_seconds") == 1.0, "the end card is added")
    info = lib.probe(os.path.join(edir, "clips", "clip-01.mp4"))
    ok((info["width"], info["height"]) == (360, 640), "clip size")
    ok(abs(info["duration"] - (man["clips"][0]["seconds"])) < 0.5, "manifest seconds match the file")
    for k in ("clip-01-cover.jpg", "contact-sheet.jpg"):
        ok(os.path.isfile(os.path.join(edir, "clips", k)), f"{k} written")
    return True


def check_tests(ws, bp, edir):
    """Every rule of check_clips.py on one batch. Leaves the batch good."""
    good = rjson(bp)

    def broken(change, needle, expect=1):
        b = copy.deepcopy(good)
        change(b)
        wjson(bp, b)
        code, out = run("check_clips.py", bp, cwd=ws, expect=expect)
        has(out, needle, f"check_clips: {needle}")
        return out
    broken(lambda b: b["clips"][0].update(why="good"), "say why this moment was picked")
    broken(lambda b: b["clips"][0]["captions"].update(TIKTOK="x" * 2300), "the limit is 2200")
    broken(lambda b: b["clips"][0]["captions"].update(INSTAGRAM="a #b #c #d #e #f #g"), "at most 5 hashtags")
    broken(lambda b: b["clips"][0]["captions"].pop("YOUTUBE"), "no YouTube Shorts caption")
    broken(lambda b: b["clips"][0].update(title="x" * 120), "YouTube title")
    broken(lambda b: b["clips"][0]["captions"].update(TIKTOK="With Jon Smyth today"), "the client spells it")
    broken(lambda b: b["clips"][2]["captions"].update(TIKTOK="A word from Acme"), "must say it's paid")
    broken(lambda b: b["episode"].update(guest_release="missing"), "no release on file")
    plain_clip(os.path.join(edir, "source.mp4"), os.path.join(edir, "own2", "nogo.mp4"), 48.0, 6.0)
    plain_clip(os.path.join(edir, "source.mp4"), os.path.join(edir, "own2", "music.mp4"), 39.0, 6.0)
    broken(lambda b: b["clips"][1].update(file="own2/nogo.mp4", start=48.0, end=54.0), "no-go list")
    broken(lambda b: b["clips"][1].update(file="own2/nogo.mp4", start=48.0, end=54.0,
                                          no_go_reviewed="says the word only to rule it out"), "reviewed", expect=0)
    broken(lambda b: b["clips"][1].update(file="own2/music.mp4", start=39.0, end=45.0), "possible music", expect=0)
    broken(lambda b: (b["clips"][1].update(file="own2/music.mp4", start=39.0, end=45.0),
                      b["clips"].append(dict(b["clips"][1], id="04"))), "same file as clip 02")
    if not good["clips"][0].get("file"):
        broken(lambda b: b["clips"][0].update(hook="A new hook nobody cut"), "changed after this clip was cut")
        broken(lambda b: b["clips"][0].update(end=13.0), "changed after this clip was cut")
    broken(lambda b: b["episode"].update(sponsor_segments=[]), "sounds like a sponsor read", expect=0)
    broken(lambda b: b["clips"].clear(), "no clips in batch.json yet")
    wjson(bp, good)
    code, out = run("check_clips.py", bp, cwd=ws)
    has(out, "paid partnership with Acme", "sponsor note")
    has(out, "WoopSocial can't set them", "manual paid labels")
    has(out, "batch fingerprint", "fingerprint shown")


def write_reply(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding) as fh:
        fh.write(text)
    return path


def approve_tests(ws, bp, edir, d):
    """The page, the plain-text reply, refusals, received time, encodings. Returns the batch fingerprint."""
    good = rjson(bp)
    bad = copy.deepcopy(good)
    bad["clips"][0]["captions"]["TIKTOK"] = "With Jon Smyth"
    wjson(bp, bad)
    refused("approve.py", bp, cwd=ws, needle="No page was made")
    ok(not os.path.exists(os.path.join(edir, "approval")), "no page when a check fails")
    wjson(bp, good)
    refused("approve.py", bp, "--sent", cwd=ws, needle="no approval page yet")
    _, out = run("approve.py", bp, cwd=ws)
    page = os.path.join(edir, "approval", "approve-ep-41.html")
    ok(os.path.isfile(page), "the page is written")
    html = open(page, encoding="utf-8").read()
    ok("data:video/mp4" not in html, "by default the page links the clips, it doesn't carry them")
    ok('href="../clips/clip-01.mp4"' in html or 'href="../own/clip-01.mp4"' in html, "the clip is linked from the page")
    ok("Planned around Mon 02 Nov, 09:00 New York time (EST)" in html, "planned time in the client's words and zone")
    ok("Nothing is posted until you reply" in html, "the hold rule on the page")
    ok('<pre id="tmpl">Page 1, Alpha Show ep-41\n1:\n2:\n3:\nTikTok:\nName:</pre>' in html, "the reply template")
    ok("mailto:agency@example.com?subject=" in html, "a plain mailto link, no script needed")
    ok('class="helper" hidden' in html, "the script helper is hidden unless scripts run")
    ok("<script src" not in html and "http://" not in html, "self-contained, no outside scripts")
    ok("isn't possible for a paid partnership" in html, "Only me is off for a sponsored batch")
    ok("we never choose them for you" in html and "checked" not in html, "TikTok: nothing chosen in advance")
    # </script> in a name can't break out of the helper's data
    c = rjson(os.path.join(ws, "clients", "alpha", "client.json"))
    c2 = copy.deepcopy(c)
    c2["name"] = "Alpha </script><b>x"
    wjson(os.path.join(ws, "clients", "alpha", "client.json"), c2)
    run("approve.py", bp, cwd=ws)
    ok(open(page, encoding="utf-8").read().count("</script>") == 1, "</script> in the data is escaped")
    wjson(os.path.join(ws, "clients", "alpha", "client.json"), c)
    # the other ways to show the clips
    run("approve.py", bp, "--embed", cwd=ws)
    ok("data:video/mp4;base64," in open(page, encoding="utf-8").read(), "--embed puts small clips inside the page")
    refused("approve.py", bp, "--clips-url", "http://example.com/x", cwd=ws, needle="https link")
    run("approve.py", bp, "--clips-url", "https://example.com/share", cwd=ws)
    ok('href="https://example.com/share/clip-01.mp4"' in open(page, encoding="utf-8").read(), "--clips-url links each clip")
    run("approve.py", bp, cwd=ws)
    led = lib.load_ledger(os.path.join(HOME[0], "ledger.json"))
    appr = rjson(os.path.join(edir, "approvals.json"))
    fp = appr["pages"][-1]["fp"]
    ok(len(appr["pages"]) == 1 and appr["pages"][0]["seq"] == 1, "the same batch made again is one page")
    ok(appr["pages"][0]["rule"] == {"rule": "hold"}, "the page stores the approval rule it printed")
    ok(all(led["clips"].get(x["sha256"], {}).get("client") == "alpha" for x in appr["pages"][-1]["clips"].values()),
       "the page claims each clip file for the client in the ledger")
    run("approve.py", bp, "--sent", "--at", "2026-11-02 04:30", cwd=ws)
    appr = rjson(os.path.join(edir, "approvals.json"))
    ok(appr["pages"][-1]["sent"] == "2026-11-02T09:30:00Z", f"--at in the client's zone: {appr['pages'][-1]['sent']}")
    # replies that are refused, and nothing recorded
    reply = os.path.join(d, "reply.txt")
    head = "Page 1, Alpha Show ep-41\n"
    for text, needle in [("Thanks, looks great!\nName: Pat Test", "no answers found"),
                         (head + "1: yes\n4: yes\nName: Pat Test", "the page has 3 clips"),
                         (head + "1: yes\n1: skip\nName: Pat Test", "answered twice"),
                         (head + "2: change\nName: Pat Test", "what should change"),
                         (head + "1: yes\n2: yes\n3: yes\nName: Mallory", "isn't one of"),
                         (head + "All yes but skip 3, the audio is off\nName: Pat Test", "says yes to all, but also"),
                         (head + "1: yes but change the caption\n2: yes\n3: yes\nName: Pat Test", "unclear"),
                         (head + "1: yes\n2: not sure\n3: yes\nName: Pat Test", "couldn't read"),
                         (head + "1: yes\nName: Pat Test", "clip 2, 3 on page 1 have no answer"),
                         ("Page 2\n1: yes\nName: Pat Test", "has no page 2"),
                         (head + "1: yes\n2: yes\n3: yes", "who replied")]:
        refused("approve.py", bp, "--apply", write_reply(reply, text), cwd=ws, needle=needle)
    ok(not rjson(os.path.join(edir, "approvals.json"))["decisions"], "nothing recorded from a refused reply")
    ok(rjson(os.path.join(edir, "approvals.json"))["pages"][0].get("held"), "a refused reply is kept as a reply that came in")
    _, out = run("approve.py", bp, "--apply", write_reply(reply, head + "All yes but skip 3\nName: Pat Test"), cwd=ws,
                 expect=1)
    has(out, "line by line, as read", "a refused reply shows what was understood")
    # the reply: a quoted line from the page is ignored, the time the client sent it is kept apart
    text = (head + "1: yes\n2: change: shorter hook\n3 ok\nTikTok: everyone, comments on, duets off, stitches off\n"
            "Name: Pat Test\n\n> 1: skip\n> 2:")
    _, out = run("approve.py", bp, "--apply", write_reply(reply, text), "--received", "2026-11-02 05:30", cwd=ws,
                 now="2026-11-02T10:45:00Z")
    has(out, "clip 2 (02): change (shorter hook)", "the change note")
    has(out, "TikTok: everyone; comments on, duets off, stitches off", "the TikTok line")
    appr = rjson(os.path.join(edir, "approvals.json"))
    d1 = appr["decisions"]["01"]
    ok(d1["decision"] == "approve" and d1["by"] == "Pat Test" and d1["by_source"].startswith("the Name"),
       f"approval recorded with how the name was given: {d1}")
    ok(d1["received"] == "2026-11-02T10:30:00Z" and d1["at"] == "2026-11-02T10:45:00Z",
       "the client's reply time is kept apart from the time it was applied")
    ok(appr["decisions"]["03"]["decision"] == "approve", "'3 ok' approves")
    ok(appr["tiktok"]["privacyLevel"] == "PUBLIC_TO_EVERYONE" and appr["tiktok"]["allowDuet"] is False, "TikTok recorded")
    refused("approve.py", bp, "--apply", reply, cwd=ws, needle="applied before")
    # a TikTok line that leaves something out records no TikTok choice
    _, out = run("approve.py", bp, "--apply", write_reply(reply, head + "1: yes\nTikTok: followers\nName: Pat Test"),
                 "--again", cwd=ws, now="2026-11-02T10:50:00Z")
    has(out, "doesn't say comments, duets, stitches", "an incomplete TikTok line")
    ok(rjson(os.path.join(edir, "approvals.json"))["tiktok"]["privacyLevel"] == "PUBLIC_TO_EVERYONE",
       "an incomplete TikTok line changes nothing")
    # replies saved by other mail apps: UTF-16 with a mark, Windows-1252; the Date line gives the time
    _, out = run("approve.py", bp, "--apply", write_reply(reply, head + "1: yes\n3: yes\nFrom: Pat Test <pat@example.com>\n"
                                                         "Date: Mon, 02 Nov 2026 06:00:00 -0500\n", "utf-16"),
                 "--again", cwd=ws, now="2026-11-02T11:00:00Z")
    has(out, "received Mon 02 Nov 06:00", "UTF-16 reply with a Date line")
    _, out = run("approve.py", bp, "--apply", write_reply(reply, head + "2: change: café scène shorter\n", "cp1252"),
                 "--by", "pat@example.com", cwd=ws, now="2026-11-02T11:01:00Z")
    has(out, "café scène shorter", "Windows-1252 reply")
    ok(rjson(os.path.join(edir, "approvals.json"))["decisions"]["02"]["by_source"].startswith("entered by the agency"),
       "--by is recorded as entered by the agency")
    # a change of mind and back again is recorded each time; only a reply that changes nothing is refused
    for i, (txt, dec) in enumerate([("1: skip, not this one", "reject"), ("1: yes", "approve"),
                                    ("1: skip, not this one", "reject"), ("1: yes", "approve")]):
        _, out = run("approve.py", bp, "--apply", write_reply(reply, head + txt + "\nName: Pat Test"), cwd=ws,
                     now=f"2026-11-02T11:0{2 + i}:00Z")
        ok(rjson(os.path.join(edir, "approvals.json"))["decisions"]["01"]["decision"] == dec,
           f"change of mind {i + 1} recorded: {out[-400:]}")
    refused("approve.py", bp, "--apply", write_reply(reply, head + "1: yes\nName: Pat Test"), cwd=ws,
            needle="changes nothing", now="2026-11-02T11:07:00Z")
    return fp


def schedule_tests(ws, bp, edir, d, fp):
    led_path = os.path.join(HOME[0], "ledger.json")
    cpath = os.path.join(ws, "clients", "alpha", "client.json")
    _, out = run("schedule.py", bp, cwd=ws, now="2026-11-02T11:00:00Z")
    has(out, "clip 02: the client asked for a change", "a clip with changes asked isn't scheduled")
    has(out, "Files to upload", "files to upload listed")
    ok(not os.path.exists(os.path.join(edir, "requests")), "no requests before the uploads")
    has(out, "paid partnership label", "manual paid label steps for the sponsored clip")
    sched = open(os.path.join(edir, "schedule.md"), encoding="utf-8").read()
    ok("Mon 02 Nov 09:00 America/New_York (EST)" in sched and "Wed 04 Nov 09:00" in sched, "times in the client's zone")
    files = {}
    b = rjson(bp)
    for c in b["clips"]:
        f = c.get("file") or f"clips/clip-{c['id']}.mp4"
        files[f] = lib.sha256(os.path.join(edir, f))
    media = {f: {"mediaId": f"media-{i}", "sha256": s[:16]} for i, (f, s) in enumerate(files.items())}
    wjson(os.path.join(edir, "media.json"), {k: v for k, v in list(media.items())[:1]})
    refused("schedule.py", bp, "--media", "media.json", cwd=ws, needle="has no mediaId", now="2026-11-02T11:00:00Z")
    wrong = dict(media)
    wrong[list(wrong)[0]] = {"mediaId": "m", "sha256": "0" * 16}
    wjson(os.path.join(edir, "media.json"), wrong)
    refused("schedule.py", bp, "--media", "media.json", cwd=ws, needle="changed after it was uploaded",
            now="2026-11-02T11:00:00Z")
    wjson(os.path.join(edir, "media.json"), media)
    wjson(os.path.join(edir, "uploaded.json"), {os.path.basename(list(files)[0]): {"sha256": "1" * 64}})
    refused("schedule.py", bp, "--media", "media.json", cwd=ws, needle="aren't this file", now="2026-11-02T11:00:00Z")
    os.remove(os.path.join(edir, "uploaded.json"))
    run("schedule.py", bp, "--media", "media.json", cwd=ws, now="2026-11-02T11:00:00Z")
    rdir = os.path.join(edir, "requests")
    ok(sorted(os.listdir(rdir)) == ["clip-01.json", "clip-03.json", "index.json"], f"requests: {os.listdir(rdir)}")
    led = lib.load_ledger(led_path)
    ok(sorted(led["planned"]) == ["alpha|ep-41|01", "alpha|ep-41|03"], f"planned slots in the ledger: {led['planned']}")
    # a reply applied after the requests were written removes them and their planned slots
    run("approve.py", bp, "--apply", write_reply(os.path.join(d, "reply.txt"), "Page 1\n1: yes\n3: yes\nName: Pat Test"),
        "--again", cwd=ws, now="2026-11-02T11:00:00Z")
    ok(not os.path.exists(os.path.join(rdir, "clip-01.json")) and not os.path.exists(os.path.join(rdir, "clip-03.json")),
       f"--apply removes the requests written before it: {os.listdir(rdir) if os.path.isdir(rdir) else []}")
    ok(not lib.load_ledger(led_path)["planned"], "--apply removes their planned slots")
    run("schedule.py", bp, "--media", "media.json", cwd=ws, now="2026-11-02T11:00:00Z")
    r1 = rjson(os.path.join(rdir, "clip-01.json"))
    r3 = rjson(os.path.join(rdir, "clip-03.json"))
    ok(r1["schedule"] == {"type": "SCHEDULE_FOR_LATER", "scheduledFor": "2026-11-02T14:00:00Z"}, f"clip 01 time {r1['schedule']}")
    ok(r3["schedule"]["scheduledFor"] == "2026-11-04T14:00:00Z", f"clip 03 time {r3['schedule']}")
    ids = {k["socialAccountId"] for k in r1["socialAccounts"]}
    ok(ids == {"acc-alpha-tt", "acc-alpha-ig", "acc-alpha-yt"}, f"only alpha's accounts: {ids}")
    tt1 = next(k for k in r1["socialAccounts"] if k["platform"] == "TIKTOK")
    tt3 = next(k for k in r3["socialAccounts"] if k["platform"] == "TIKTOK")
    ok(tt1["privacyLevel"] == "PUBLIC_TO_EVERYONE" and tt1["allowComment"] is True and tt1["allowDuet"] is False,
       "the client's TikTok choices in the request")
    ok(tt1["isBrandedContent"] is False and tt3["isBrandedContent"] is True, "paid partnership label on the sponsor clip")
    ok(all(k["autoAddMusic"] is False for k in (tt1, tt3)), "autoAddMusic false")
    yt = next(k for k in r1["socialAccounts"] if k["platform"] == "YOUTUBE")
    ok(yt["privacy"] == "public" and yt["madeForKids"] is False and yt["title"] == "The first idea", "YouTube fields")
    ig = next(k for k in r1["socialAccounts"] if k["platform"] == "INSTAGRAM")
    ok(ig["postType"] == "REEL" and ig["contentOverride"][0]["text"].endswith("#test"), "Instagram caption per platform")
    # the gate right before sending
    _, out = run("schedule.py", bp, "--check", "requests/clip-01.json", cwd=ws, now="2026-11-02T11:00:00Z")
    has(out, "ok to send: clip-01.json", "--check passes a good request")
    refused("schedule.py", bp, "--check", "requests/clip-01.json", cwd=ws, now="2026-11-02T13:45:00Z",
            needle="less than 30 minutes")
    b2 = copy.deepcopy(b)
    b2["episode"]["guest_release"] = "missing"
    wjson(bp, b2)
    refused("schedule.py", bp, "--check", "requests/clip-01.json", cwd=ws, needle="fails a check now")
    wjson(bp, b)
    c = rjson(cpath)
    wjson(cpath, dict(c, status="paused"))
    refused("schedule.py", bp, "--check", "requests/clip-01.json", cwd=ws, needle="paused")
    refused("schedule.py", bp, "--posted", "requests/clip-01.json", "p1", cwd=ws, needle="paused")
    wjson(cpath, c)
    # --posted: refusals first
    refused("schedule.py", bp, "--posted", "requests/clip-01.json", " ", cwd=ws, needle="give the post id")
    refused("schedule.py", bp, "--posted", "requests/clip-02.json", "p2", cwd=ws, needle="isn't a request")
    edited = copy.deepcopy(r1)
    edited["schedule"]["scheduledFor"] = "2026-11-02T15:00:00Z"
    wjson(os.path.join(rdir, "clip-01.json"), edited)
    refused("schedule.py", bp, "--check", "requests/clip-01.json", cwd=ws, needle="was edited after")
    wjson(os.path.join(rdir, "clip-01.json"), r1)
    led = lib.load_ledger(led_path)
    led["accounts"]["acc-alpha-ig"] = "beta"
    wjson(led_path, led)
    refused("schedule.py", bp, "--posted", "requests/clip-01.json", "p1", cwd=ws, needle="belongs to client beta")
    led["accounts"].pop("acc-alpha-ig")
    wjson(led_path, led)
    _, out = run("schedule.py", bp, "--posted", "requests/clip-01.json", "post-1", cwd=ws, now="2026-11-02T11:05:00Z")
    has(out, "recorded: clip 01 is post post-1", "posted")
    refused("schedule.py", bp, "--posted", "requests/clip-01.json", "post-1", cwd=ws, needle="already")
    refused("schedule.py", bp, "--check", "requests/clip-01.json", cwd=ws, needle="already handed to WoopSocial")
    led = lib.load_ledger(led_path)
    e = led["posts"]["post-1|acc-alpha-tt"]
    ok(e["client"] == "alpha" and e["status"] == "scheduled" and e["approved_by"].lower() == "pat test", f"ledger post entry {e}")
    ok("alpha|ep-41|01" not in led["planned"] and "alpha|ep-41|03" in led["planned"], "a sent request leaves the planned list")
    # a caption edited after the approval: the request for it is refused, and the clip waits for a new page
    b = rjson(bp)
    b["clips"][2]["captions"]["TIKTOK"] = "#ad Sponsored by Acme. Edited after the yes."
    wjson(bp, b)
    refused("schedule.py", bp, "--posted", "requests/clip-03.json", "post-3", cwd=ws, needle="no recorded approval")
    _, out = run("approve.py", bp, "--status", cwd=ws, now="2026-11-02T11:10:00Z")
    has(out, "clip 03: approve by Pat Test, then the clip changed", "status shows the void approval")
    _, out = run("schedule.py", bp, cwd=ws, now="2026-11-02T11:10:00Z")
    has(out, "clip 03: changed after the client approved it", "a re-edited clip isn't scheduled")
    ok(not os.path.exists(os.path.join(rdir, "clip-03.json")), "the old request was removed")
    has(out, "clip 01: scheduled as post `post-1`", "the posted clip is listed")
    # a new page: requests written before it go (a stand-in request here), the earlier yes and the time show
    wjson(os.path.join(rdir, "clip-09.json"), {"stand-in": True})
    wjson(os.path.join(rdir, "index.json"), {"client": "alpha", "episode": "ep-41", "requests": {"clip-09.json": {}}})
    led = lib.load_ledger(led_path)
    led["planned"]["alpha|ep-41|09"] = {"client": "alpha", "episode": "ep-41", "clip": "09",
                                        "scheduledFor": "2026-11-09T14:00:00Z", "accounts": []}
    wjson(led_path, led)
    _, out = run("approve.py", bp, cwd=ws, now="2026-11-02T11:15:00Z")
    appr = rjson(os.path.join(edir, "approvals.json"))
    fp2 = appr["pages"][-1]["fp"]
    ok(fp2 != fp and [p["seq"] for p in appr["pages"]] == [1, 2], "a changed batch gets page 2")
    ok(not [n for n in os.listdir(rdir) if n.startswith("clip-")], "a new page removes the requests written before it")
    ok("alpha|ep-41|09" not in lib.load_ledger(led_path)["planned"], "and their planned slots")
    html = open(os.path.join(edir, "approval", "approve-ep-41.html"), encoding="utf-8").read()
    ok("You approved this clip on page 1" in html, "the new page shows the earlier yes")
    ok("Already scheduled for Mon 02 Nov, 09:00 New York time (EST). Reply &quot;1: skip&quot;" in html,
       "the scheduled clip keeps its time and says how to stop it")
    ok("Your choice from page 1: everyone" in html, "the page shows the TikTok choice that still holds")
    # an older page's answers never override a newer page; a reply naming no page is refused when there are two
    reply = os.path.join(d, "reply2.txt")
    # page 2 was made but not sent yet: a late reply to page 1 still counts, and says page 2 doesn't
    _, out = run("approve.py", bp, "--apply", write_reply(reply, "Page 1, Alpha Show ep-41\n3: yes\nName: Pat Test"),
                 "--partial", cwd=ws, now="2026-11-02T11:15:30Z")
    has(out, "page 2 was made after page 1 but never sent", "an unsent newer page doesn't block a reply to page 1")
    run("approve.py", bp, "--sent", cwd=ws, now="2026-11-02T11:16:00Z")
    refused("approve.py", bp, "--apply", write_reply(reply, "Page 1, Alpha Show ep-41\n3: no changes\nName: Pat Test"),
            cwd=ws, needle="page 2 is newer and was sent")
    refused("approve.py", bp, "--apply", write_reply(reply, "3: yes\nName: Pat Test"), cwd=ws, needle="which page")
    _, out = run("approve.py", bp, "--apply", write_reply(reply, "Page 2\n2: yes\n3: yes\nTikTok: followers, comments on, "
                                                          "duets on, stitches off\nName: Pat Test"),
                 cwd=ws, now="2026-11-02T11:25:00Z")
    has(out, "clip 3 (03): approve", "the new page's answer")
    has(out, "changed: clip 2 (02): change -> approve", "a changed decision is said")
    has(out, "time the client replied: unknown", "no received time: unknown, not the apply time")
    run("schedule.py", bp, "--media", "media.json", cwd=ws, now="2026-11-02T11:30:00Z")
    r2 = rjson(os.path.join(rdir, "clip-02.json"))
    r3 = rjson(os.path.join(rdir, "clip-03.json"))
    t2, t3 = r2["schedule"]["scheduledFor"], r3["schedule"]["scheduledFor"]
    ok((t2, t3) == ("2026-11-04T14:00:00Z", "2026-11-06T14:00:00Z"), f"spread after the post already scheduled: {t2} {t3}")
    tt2 = next(k for k in r2["socialAccounts"] if k["platform"] == "TIKTOK")
    ok(tt2["privacyLevel"] == "FOLLOWER_OF_CREATOR" and tt2["allowDuet"] is True, "the newest TikTok choice")
    run("schedule.py", bp, "--posted", "requests/clip-02.json", "post-2", cwd=ws, now="2026-11-02T11:34:00Z")
    run("schedule.py", bp, "--posted", "requests/clip-03.json", "post-3", cwd=ws, now="2026-11-02T11:35:00Z")
    # a skip on a scheduled clip whose row was edited after the page still cancels the post the client saw
    keep_b, keep_led = rjson(bp), open(led_path, encoding="utf-8").read()
    keep_appr = open(os.path.join(edir, "approvals.json"), encoding="utf-8").read()
    b3 = copy.deepcopy(keep_b)
    b3["clips"][2]["captions"]["INSTAGRAM"] += " Typo fixed."
    wjson(bp, b3)
    _, out = run("approve.py", bp, "--apply", write_reply(reply, "Page 2\n3: skip, we can't use it\nName: Pat Test"),
                 cwd=ws, now="2026-11-02T11:40:00Z")
    has(out, "CANCEL NOW: post post-3", "a skip on an edited, scheduled clip still cancels its post")
    ok({e["status"] for k, e in lib.load_ledger(led_path)["posts"].items() if k.startswith("post-3|")} == {"cancel_requested"},
       "the edited clip's post waits for its cancel")
    wjson(bp, keep_b)
    with open(led_path, "w", encoding="utf-8") as fh:
        fh.write(keep_led)
    with open(os.path.join(edir, "approvals.json"), "w", encoding="utf-8") as fh:
        fh.write(keep_appr)
    if os.path.exists(os.path.join(rdir, "cancel-post-3.json")):
        os.remove(os.path.join(rdir, "cancel-post-3.json"))
    # the client says skip on a clip that is already scheduled: it must be cancelled
    _, out = run("approve.py", bp, "--apply", write_reply(reply, "Page 2\n2: skip\nName: Pat Test"), cwd=ws,
                 now="2026-11-02T12:00:00Z")
    has(out, "CANCEL NOW: post post-2", "a skip on a scheduled clip says to cancel it")
    led = lib.load_ledger(led_path)
    ok({e["status"] for k, e in led["posts"].items() if k.startswith("post-2|")} == {"cancel_requested"},
       "the scheduled post waits for its cancel")
    ok(rjson(os.path.join(rdir, "cancel-post-2.json")) == {"tool": "posts_delete", "postId": "post-2",
                                                           "why": "client said skip on page 2"}, "the delete request")
    _, out = run("approve.py", bp, "--status", cwd=ws, now="2026-11-02T12:01:00Z")
    has(out, "CANCEL post post-2", "status names the cancel")
    _, out = run("status.py", "alpha", cwd=ws, now="2026-11-02T12:01:00Z")
    has(out, "cancel post post-2 now", "status.py names the cancel")
    _, out = run("schedule.py", bp, cwd=ws, now="2026-11-02T12:02:00Z")
    has(out, "CANCEL post `post-2` now", "schedule.md names the cancel")
    # a routine status refresh never clears a pending cancel
    wjson(os.path.join(d, "st2.json"), {"postId": "post-2", "socialAccountPosts": [
        {"socialAccountId": f"acc-alpha-{p}", "deliveryStatus": "NOT_STARTED"} for p in ("tt", "ig", "yt")]})
    _, out = run("schedule.py", bp, "--status", os.path.join(d, "st2.json"), cwd=ws, now="2026-11-02T12:03:00Z")
    has(out, "CANCEL NOW: post post-2", "the status refresh says the cancel again")
    ok({e["status"] for k, e in lib.load_ledger(led_path)["posts"].items() if k.startswith("post-2|")} == {"cancel_requested"},
       "NOT_STARTED doesn't turn a pending cancel back into scheduled")
    ok(os.path.isfile(os.path.join(rdir, "cancel-post-2.json")), "the delete request stays")
    _, out = run("status.py", "alpha", cwd=ws, now="2026-11-02T12:03:00Z")
    has(out, "cancel post post-2 now", "status.py still names the cancel after a refresh")
    # too late: one account went live before the cancel
    keep_led = open(led_path, encoding="utf-8").read()
    wjson(os.path.join(d, "st3.json"), {"postId": "post-2", "socialAccountPosts": [
        {"socialAccountId": "acc-alpha-tt", "deliveryStatus": "PUBLISHED", "externalPostUrl": "https://www.tiktok.com/@alpha/video/2"},
        {"socialAccountId": "acc-alpha-ig", "deliveryStatus": "NOT_STARTED"},
        {"socialAccountId": "acc-alpha-yt", "deliveryStatus": "NOT_STARTED"}]})
    _, out = run("schedule.py", bp, "--status", os.path.join(d, "st3.json"), cwd=ws, now="2026-11-02T12:04:00Z")
    has(out, "LIVE, NOT CANCELLED: clip 02 went live on TIKTOK", "a post live after the client said no is said plainly")
    has(out, "POST_NOT_DELETABLE", "WoopSocial can't delete a live post")
    has(out, "will go out too", "the other accounts of a part-live post")
    led = lib.load_ledger(led_path)
    ok(led["posts"]["post-2|acc-alpha-tt"]["status"] == "published"
       and led["posts"]["post-2|acc-alpha-tt"].get("live_after_cancel_request")
       and led["posts"]["post-2|acc-alpha-ig"]["status"] == "cancel_requested", "live part and waiting parts recorded")
    _, out = run("status.py", "alpha", cwd=ws, now="2026-11-02T12:04:00Z")
    has(out, "went live although the client said no", "status.py names the live post to remove")
    with open(led_path, "w", encoding="utf-8") as fh:
        fh.write(keep_led)
    refused("schedule.py", bp, "--cancelled", "post-9", cwd=ws, needle="isn't in the ledger")
    _, out = run("schedule.py", bp, "--cancelled", "post-2", cwd=ws, now="2026-11-02T12:05:00Z")
    has(out, "recorded as cancelled: post post-2", "the cancel recorded")
    ok(not os.path.exists(os.path.join(rdir, "cancel-post-2.json")), "the delete request is gone once done")
    ok({e["status"] for k, e in lib.load_ledger(led_path)["posts"].items() if k.startswith("post-2|")} == {"cancelled"},
       "cancelled in the ledger")
    # the status of a post
    wjson(os.path.join(d, "status.json"), {"postId": "post-1", "socialAccountPosts": [
        {"socialAccountId": "acc-alpha-tt", "deliveryStatus": "PUBLISHED", "deliveryCompletedAt": "2026-11-02T14:00:20Z",
         "externalPostUrl": "https://www.tiktok.com/@alpha/video/1"},
        {"socialAccountId": "acc-alpha-ig", "deliveryStatus": "PUBLISHED", "deliveryCompletedAt": "2026-11-02T14:00:30Z",
         "externalPostUrl": "https://www.instagram.com/reel/A1/"},
        {"socialAccountId": "acc-alpha-yt", "deliveryStatus": "FAILED", "errorMessage": "token expired"}]})
    refused("schedule.py", bp, "--status", os.path.join(d, "status.json"), "post-9", cwd=ws, needle="not post-9")
    _, out = run("schedule.py", bp, "--status", os.path.join(d, "status.json"), cwd=ws, now="2026-11-02T15:00:00Z")
    has(out, "clip 01 on TIKTOK: published https://www.tiktok.com/@alpha/video/1", "status published")
    has(out, "failed: YOUTUBE: token expired", "status failed with the reason")
    led = lib.load_ledger(led_path)
    ok(led["posts"]["post-1|acc-alpha-yt"]["status"] == "failed", "failed recorded")
    refused("schedule.py", bp, "--cancelled", "post-1", cwd=ws, needle="already live")


def beta_tests(ws, d, alpha_edir):
    """The second client: the same clip file is refused, a rule changed after sending, approval on silence."""
    spec = [dict(c, file=f"own/clip-{c['id']}.mp4") for c in clips_spec()[:2]]
    for c in spec:
        c["captions"] = {"TIKTOK": c["captions"]["TIKTOK"], "YOUTUBE": c["captions"]["YOUTUBE"]}
    spec[0]["start"], spec[0]["end"] = 2.0, 8.0
    spec[1]["start"], spec[1]["end"] = 39.0, 45.0   # holds the [MUSIC] cue
    bp, edir = build_batch(ws, d, "beta", "ep-9", True, spec=spec)
    b = rjson(bp)
    b["episode"]["sponsor_segments"] = []
    b["episode"]["guests"] = []
    b["episode"]["guest_release"] = "not_needed"
    wjson(bp, b)
    # alpha's clip file handed to beta: refused (the ledger claimed it when alpha's page was made)
    alpha_clip = os.path.join(alpha_edir, rjson(os.path.join(alpha_edir, "batch.json"))["clips"][0].get("file")
                              or "clips/clip-01.mp4")
    keep = os.path.join(d, "beta-01.mp4")
    shutil.copy(os.path.join(edir, "own", "clip-01.mp4"), keep)
    shutil.copy(alpha_clip, os.path.join(edir, "own", "clip-01.mp4"))
    code, out = run("check_clips.py", bp, cwd=ws, expect=1)
    has(out, "already went to client alpha", "the same clip for two clients")
    refused("approve.py", bp, cwd=ws, needle="No page was made")
    shutil.copy(keep, os.path.join(edir, "own", "clip-01.mp4"))
    code, out = run("check_clips.py", bp, cwd=ws)
    has(out, "possible music", "music warning from the caption cue")
    has(out, "faster-whisper isn't installed" if not have_whisper() else "listened to", "the edge check says what it did")
    # page 1 is sent under the hold rule; the rule changed to silence later doesn't approve it
    cpath = os.path.join(ws, "clients", "beta", "client.json")
    auto = rjson(cpath)
    wjson(cpath, dict(auto, approval={"rule": "hold"}))
    run("approve.py", bp, cwd=ws, now="2026-11-02T10:00:00Z")
    run("approve.py", bp, "--sent", cwd=ws, now="2026-11-02T10:00:00Z")
    wjson(cpath, auto)
    _, out = run("schedule.py", bp, cwd=ws, now="2026-11-03T10:30:00Z")
    has(out, "waiting for the client", "a rule changed after sending doesn't approve the page sent before")
    ok(not os.path.exists(os.path.join(edir, "requests")), "no request for unapproved clips")
    # a hand-made request is refused
    clip = rjson(bp)["clips"][0]
    sha = lib.sha256(os.path.join(edir, "own", "clip-01.mp4"))
    body = {"content": [{"text": "x", "media": [{"type": "MEDIA_LIBRARY", "mediaId": "m"}]}],
            "schedule": {"type": "SCHEDULE_FOR_LATER", "scheduledFor": "2026-11-05T13:00:00Z"},
            "socialAccounts": [{"platform": "YOUTUBE", "socialAccountId": "acc-beta-yt", "title": "t", "privacy": "public",
                                "madeForKids": False, "contentOverride": [{"text": "x"}]}]}
    wjson(os.path.join(edir, "requests", "clip-01.json"), body)
    wjson(os.path.join(edir, "requests", "index.json"), {"client": "beta", "episode": "ep-9", "requests": {"clip-01.json": {
        "clip": "01", "hash": lib.clip_hash(sha, clip), "sha256": sha, "body_sha256": body_sha(body),
        "scheduledFor": "2026-11-05T13:00:00Z", "accounts": {"acc-beta-yt": "YOUTUBE"}}}})
    refused("schedule.py", bp, "--check", "requests/clip-01.json", cwd=ws, needle="no recorded approval",
            now="2026-11-03T10:30:00Z")
    body2 = copy.deepcopy(body)
    body2["socialAccounts"][0]["socialAccountId"] = "acc-alpha-yt"
    wjson(os.path.join(edir, "requests", "clip-01.json"), body2)
    idx = rjson(os.path.join(edir, "requests", "index.json"))
    idx["requests"]["clip-01.json"]["body_sha256"] = body_sha(body2)
    wjson(os.path.join(edir, "requests", "index.json"), idx)
    refused("schedule.py", bp, "--posted", "requests/clip-01.json", "px", cwd=ws, needle="belongs to client alpha",
            now="2026-11-03T10:30:00Z")
    shutil.rmtree(os.path.join(edir, "requests"))
    # page 2 under the agreed rule: an absolute deadline on the page, silence counts only after it
    run("approve.py", bp, cwd=ws, now="2026-11-03T10:31:00Z")
    appr = rjson(os.path.join(edir, "approvals.json"))
    ok([p["seq"] for p in appr["pages"]] == [1, 2] and appr["pages"][1]["rule"]["rule"] == "auto_after_hours",
       "a new rule needs a new page, and the page stores it")
    html = open(os.path.join(edir, "approval", "approve-ep-9.html"), encoding="utf-8").read()
    has(html, "If we haven't heard from you by Wed 04 Nov, 12:00 Amsterdam time (CET)", "the deadline as a date and time")
    run("approve.py", bp, "--sent", cwd=ws, now="2026-11-03T10:31:00Z")
    _, out = run("schedule.py", bp, cwd=ws, now="2026-11-04T10:59:00Z")
    has(out, "waiting for the client", "not before the deadline on the page")
    _, out = run("approve.py", bp, "--status", cwd=ws, now="2026-11-04T11:01:00Z")
    has(out, "no reply by the deadline on page 2, as agreed in writing", "approval on silence shown as silence")
    run("approve.py", bp, cwd=ws, now="2026-11-04T11:01:00Z")
    _, out = run("schedule.py", bp, cwd=ws, now="2026-11-04T11:01:00Z")
    has(out, "TikTok waits", "TikTok isn't posted on silence: the client never chose who sees it")
    ok(rjson(os.path.join(edir, "approvals.json"))["decisions"]["01"]["how"] == "auto", "the silent approval is recorded")
    run("approve.py", bp, cwd=ws, now="2026-11-04T11:02:00Z")
    html = open(os.path.join(edir, "approval", "approve-ep-9.html"), encoding="utf-8").read()
    ok("No reply by Wed 04 Nov, 12:00" in html and "You approved" not in html, "silence is never called 'you approved'")
    media = {f"own/clip-{k}.mp4": {"mediaId": f"bm-{k}", "sha256": lib.sha256(os.path.join(edir, "own", f"clip-{k}.mp4"))}
             for k in ("01", "02")}
    wjson(os.path.join(edir, "media.json"), media)
    run("schedule.py", bp, "--media", "media.json", cwd=ws, now="2026-11-04T11:03:00Z")
    r = rjson(os.path.join(edir, "requests", "clip-01.json"))
    ok([k["platform"] for k in r["socialAccounts"]] == ["YOUTUBE"], "only YouTube goes out on silence")
    ok(r["schedule"]["scheduledFor"] == "2026-11-04T13:00:00Z", f"Amsterdam 14:00, an hour's lead at least: {r['schedule']}")
    run("schedule.py", bp, "--posted", "requests/clip-01.json", "post-b1", cwd=ws, now="2026-11-04T11:05:00Z")
    # page 3 under the same rule: an unclear reply is refused, and silence no longer approves anything on it
    b = rjson(bp)
    b["clips"][1]["captions"]["TIKTOK"] += " Edited."
    wjson(bp, b)
    run("approve.py", bp, cwd=ws, now="2026-11-04T11:10:00Z")
    run("approve.py", bp, "--sent", cwd=ws, now="2026-11-04T11:10:00Z")
    refused("approve.py", bp, "--apply", write_reply(os.path.join(d, "rb.txt"), "Page 3\nAll yes but not 2\nName: Pat Test"),
            cwd=ws, now="2026-11-04T12:00:00Z", needle="silence no longer counts for page 3")
    _, out = run("approve.py", bp, "--status", cwd=ws, now="2026-11-06T12:00:00Z")
    has(out, "waiting for the client on the new page", "after a refused reply, silence never approves the page")
    ok("clip 02: approve (" not in out, "no silent approval after a refused reply")
    has(out, "silence no longer counts", "the status says why")
    _, out = run("approve.py", bp, "--apply", write_reply(os.path.join(d, "rb.txt"), "Page 3\n1: yes\nName: Pat Test"),
                 "--partial", "--again", cwd=ws, now="2026-11-06T12:05:00Z")
    has(out, "no answer yet (--partial): clip 2", "--partial names the clips still open")
    _, out = run("approve.py", bp, "--status", cwd=ws, now="2026-11-09T12:00:00Z")
    has(out, "waiting for the client on the new page", "after a partial reply, the clips left open wait")
    ok("clip 02: approve (" not in out, "no silent approval after a partial reply")
    # the same file in another episode of the same client is refused
    bp10, edir10 = build_batch(ws, d, "beta", "ep-10", True, spec=copy.deepcopy(spec))
    b10 = rjson(bp10)
    b10["episode"].update(sponsor_segments=[], guests=[], guest_release="not_needed")
    wjson(bp10, b10)
    shutil.copy(os.path.join(edir, "own", "clip-01.mp4"), os.path.join(edir10, "own", "clip-01.mp4"))
    code, out = run("check_clips.py", bp10, "--no-listen", cwd=ws, expect=1)
    has(out, "the same file as episode ep-9 clip 01", "the same clip twice for one client")
    shutil.rmtree(edir10)
    return bp, edir


def have_whisper():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def analytics_file(d, name, items):
    wjson(os.path.join(d, name), {"content": [{"type": "text", "text": json.dumps({"socialAccountPosts": items})}]})
    return os.path.join(d, name)


def status_report_tests(ws, d):
    _, out = run("status.py", cwd=ws, now="2026-11-04T11:10:00Z")
    has(out, "Alpha Show", "status lists alpha")
    has(out, "Beta Talks", "status lists beta")
    has(out, "on the agency", "status summary")
    _, out = run("status.py", "--json", cwd=ws, now="2026-11-04T11:10:00Z")
    st = json.loads(out)
    ok({s["client"] for s in st} == {"alpha", "beta"}, "status --json")
    led_path = os.path.join(HOME[0], "ledger.json")
    T = "2026-11-20T06:00:00Z"
    items = [
        {"socialAccountId": "acc-alpha-tt", "externalPostUrl": "https://www.tiktok.com/@alpha/video/1",
         "deliveryCompletedAt": "2026-11-02T14:00:20Z", "metrics": {"views": 1000, "likes": 10}, "metricsCollectedAt": T},
        {"socialAccountId": "acc-alpha-tt", "externalPostUrl": "https://www.tiktok.com/@alpha/video/1",
         "deliveryCompletedAt": "2026-11-02T14:00:20Z", "metrics": {"views": 900}, "metricsCollectedAt": "2026-11-19T06:00:00Z"},
        {"socialAccountId": "acc-alpha-tt", "externalPostUrl": "https://www.tiktok.com/@alpha/video/3?is_from_webapp=1",
         "deliveryCompletedAt": "2026-11-06T14:00:20Z", "metrics": {"views": 3000, "likes": 30}, "metricsCollectedAt": T},
        {"socialAccountId": "acc-alpha-ig", "externalPostUrl": "https://www.instagram.com/reel/A1/",
         "deliveryCompletedAt": "2026-11-02T14:00:30Z", "metrics": {"views": 500}, "metricsCollectedAt": T},
        {"socialAccountId": "acc-alpha-ig", "externalPostUrl": "https://www.instagram.com/reel/A3/",
         "deliveryCompletedAt": "2026-11-06T14:00:30Z", "metrics": {}, "metricsCollectedAt": T},
        {"socialAccountId": "acc-alpha-yt", "externalPostUrl": "https://www.youtube.com/shorts/A3",
         "deliveryCompletedAt": "2026-11-06T14:01:00Z", "metrics": {"views": 600}, "metricsCollectedAt": T},
        {"socialAccountId": "acc-alpha-ig", "externalPostUrl": "https://www.instagram.com/reel/HANDMADE/",
         "deliveryCompletedAt": "2026-11-10T10:00:00Z", "metrics": {"views": 99999}, "metricsCollectedAt": T}]
    good = analytics_file(d, "analytics.json", items)
    stale = analytics_file(d, "analytics-stale.json", [i for i in items if "/3" not in i["externalPostUrl"]
                                                       and "A3" not in i["externalPostUrl"]])
    mixed = analytics_file(d, "analytics-mixed.json", items + [
        {"socialAccountId": "acc-beta-yt", "externalPostUrl": "https://www.youtube.com/shorts/B1",
         "metrics": {"views": 77777}, "metricsCollectedAt": T}])
    wjson(os.path.join(d, "followers.json"), {"socialAccountId": "acc-alpha-ig", "days": [
        {"date": "2026-11-01", "followers": 100}, {"date": "2026-11-30", "followers": 160}]})
    refused("report.py", "alpha", "--month", "2026-13", cwd=ws, needle="--month")
    refused("report.py", "nobody", "--month", "2026-11", cwd=ws, needle="no client")
    # post-3 is still "scheduled" in the ledger: the ledger isn't trusted, the analytics are
    refused("report.py", "alpha", "--month", "2026-11", stale, cwd=ws, now="2026-12-01T09:00:00Z",
            needle="should have gone out by now")
    _, out = run("report.py", "alpha", "--month", "2026-11", stale, "--allow-unknown", cwd=ws, now="2026-12-01T09:00:00Z")
    has(out, "couldn't confirm yet", "--allow-unknown says what is unknown")
    refused("report.py", "alpha", "--month", "2026-11", mixed, cwd=ws, now="2026-12-01T09:00:00Z",
            needle="another project's reply")
    led = lib.load_ledger(led_path)
    ok(all(e["status"] == "scheduled" for k, e in led["posts"].items() if k.startswith("post-3|")), "post-3 not refreshed yet")
    for k in led["posts"]:
        if k.startswith("post-3|"):
            led["posts"][k].pop("url", None)
    wjson(led_path, led)
    _, out = run("report.py", "alpha", "--month", "2026-11", good, "--followers", os.path.join(d, "followers.json"),
                 cwd=ws, now="2026-12-01T09:00:00Z")
    has(out, "2 clips went live, against 6 agreed for the month.", "agreed against delivered")
    has(out, "Why fewer: you skipped 1 clip; 1 post failed to publish", "the cause of the shortfall")
    has(out, "4,000 views on 2 posts. A view here is plays of the video", "TikTok: newest numbers win, matched by time")
    ok("Median clip" not in out.split("## Instagram")[0], "no median from two posts")
    has(out, "Top clip: \"A word from Acme\", 3,000 views", "the top clip by its hook, not an id")
    has(out, "500 views on 1 post. A view here is times the Reel was shown", "Instagram meaning")
    has(out, "1 post has no numbers yet", "missing numbers said, not guessed")
    has(out, "600 views on 1 post.", "YouTube: the failed post isn't counted as live")
    has(out, "| A word from Acme | alpha ep-41 | 13 |", "days live per clip")
    has(out, "Instagram Reels: 100 on 2026-11-01 to 160 on 2026-11-30 (+60)", "followers when WoopSocial gives them")
    has(out, "wasn't made by us", "posts made outside the skill")
    has(out, "never added together", "platforms never summed")
    has(out, "You replied in 1.0 hours at the median", "turnaround from the client's reply time")
    has(out, "For 1 page we didn't record when your reply arrived", "an unknown reply time is said")
    ok("99,999" not in out and "77,777" not in out, "numbers of other posts never in the totals")
    rdir = os.path.join(ws, "clients", "alpha", "reports")
    md = open(os.path.join(rdir, "2026-11.md"), encoding="utf-8").read()
    ok("post-" not in md and "acc-alpha" not in md and "schedule.py" not in md, "no internal ids or steps in the client's report")
    ag = open(os.path.join(rdir, "2026-11-agency.md"), encoding="utf-8").read()
    ok("post-3" in ag and "HANDMADE" in ag, "agency notes hold what to chase")
    html = open(os.path.join(rdir, "2026-11.html"), encoding="utf-8").read()
    ok("prefers-color-scheme:dark" in html and "<script" not in html and "max-width:640px" in html,
       "report html: dark mode, phone layout, no scripts")
    _, out = run("report.py", "alpha", "--month", "2026-12", cwd=ws, now="2027-01-02T09:00:00Z")
    has(out, "Last month (November 2026): 2 clips went live", "the previous month next to this one")
    has(out, "No view numbers were available", "a report without analytics says so")


def offboard_tests(ws, bp_alpha):
    led_path = os.path.join(HOME[0], "ledger.json")
    led = lib.load_ledger(led_path)
    for k in led["posts"]:   # one alpha post back to scheduled, to cancel it
        if k.startswith("post-3|"):
            led["posts"][k]["status"] = "scheduled"
            led["posts"][k]["scheduledFor"] = "2026-11-06T14:00:00Z"
    wjson(led_path, led)
    _, out = run("client.py", "offboard", "alpha", cwd=ws, now="2026-11-03T12:00:00Z")
    has(out, "post-3", "scheduled posts to cancel")
    ok("post-1" not in out, "live posts aren't listed to cancel")
    has(out, "social_accounts_delete", "access to remove")
    has(out, "TikTok, Instagram (through Meta's settings), your Google account for YouTube", "the client's own steps")
    has(out, "media_delete", "handover and clean-up steps")
    ok(rjson(os.path.join(ws, "clients", "alpha", "client.json"))["status"] == "offboarding", "status offboarding")
    refused("approve.py", bp_alpha, cwd=ws, needle="offboarding")
    refused("schedule.py", bp_alpha, cwd=ws, needle="offboarding")
    if not NO_CUT:
        refused("make_clips.py", bp_alpha, cwd=ws, needle="no new clips")
    refused("client.py", "offboard", "alpha", "--done", cwd=ws, needle="not done yet")
    refused("client.py", "offboard", "alpha", "--cancelled", "post-b1", cwd=ws, needle="another client")
    refused("client.py", "offboard", "alpha", "--cancelled", "post-1", cwd=ws, needle="already live")
    run("client.py", "offboard", "alpha", "--cancelled", "post-3", cwd=ws)
    refused("client.py", "offboard", "alpha", "--revoked", "PINTEREST", cwd=ws, needle="isn't a connected account")
    run("client.py", "offboard", "alpha", "--revoked", "TIKTOK", "INSTAGRAM", "YOUTUBE", cwd=ws)
    refused("client.py", "release", "acc-alpha-tt", cwd=ws, needle="Only an offboarded")
    refused("client.py", "offboard", "alpha", "--done", cwd=ws, needle="NOT_STARTED")
    run("client.py", "offboard", "alpha", "--done", "--nothing-waiting", cwd=ws)
    c = rjson(os.path.join(ws, "clients", "alpha", "client.json"))
    ok(c["status"] == "offboarded" and len(c["revoked"]) == 3, "offboarded, revoked accounts kept for the record")
    _, out = run("status.py", "alpha", cwd=ws)
    has(out, "offboarded", "status shows offboarded")
    # the report still finds the posts on the revoked accounts
    _, out = run("report.py", "alpha", "--month", "2026-11", os.path.join(os.path.dirname(ws), "analytics.json"),
                 "--allow-unknown", cwd=ws, now="2026-12-01T09:00:00Z")
    has(out, "1,000 views on 1 post", "report after offboarding")
    ag = open(os.path.join(ws, "clients", "alpha", "reports", "2026-11-agency.md"), encoding="utf-8").read()
    ok("https://www.tiktok.com/@alpha/video/3" in ag.split("Not made through this skill:")[-1],
       "a cancelled post is never matched by its time: the post in its slot isn't counted as ours")
    refused("client.py", "release", "acc-beta-tt", cwd=ws, needle="Only an offboarded")
    _, out = run("client.py", "release", "acc-alpha-tt", cwd=ws)
    has(out, "released acc-alpha-tt", "an offboarded client's account can be released")
    ok("acc-alpha-tt" not in lib.load_ledger(led_path)["accounts"], "released in the ledger")


def damaged_tests(ws, d, bp):
    led_path = os.path.join(HOME[0], "ledger.json")
    keep = open(led_path, encoding="utf-8").read()
    with open(led_path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    refused("status.py", cwd=ws, needle="ledger")
    with open(led_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"clips": [], "accounts": {}, "posts": {}}))
    refused("check_clips.py", bp, cwd=ws, needle="damaged")
    with open(led_path, "w", encoding="utf-8") as fh:
        fh.write(keep)
    with open(os.path.join(d, "broken.json"), "w") as fh:
        fh.write("[1, 2")
    refused("check_clips.py", os.path.join(d, "broken.json"), cwd=ws)
    refused("approve.py", os.path.join(d, "nothere.json"), cwd=ws)
    refused("status.py", "--workspace", os.path.join(d, "empty"), cwd=d, needle="no clients")


def upload_tests(d):
    refused("upload_parts.py", cwd=d, needle="usage")
    refused("upload_parts.py", "nothere.json", "a.mp4", cwd=d, needle="not found")
    shutil.copy(os.path.join(d, "source.mp4"), os.path.join(d, "a.mp4"))
    wjson(os.path.join(d, "session.json"), {"partSizeInBytes": 10, "parts": [{"partNumber": 1, "uploadUrl": "http://x.test"}]})
    refused("upload_parts.py", "session.json", "a.mp4", cwd=d, needle="https")
    wjson(os.path.join(d, "session.json"), {"something": "else"})
    refused("upload_parts.py", "session.json", "a.mp4", cwd=d, needle="media_uploads_create_session")
    # the success path, with the network put replaced: every byte goes out in order, and uploaded.json is written
    size = os.path.getsize(os.path.join(d, "a.mp4"))
    part = size // 3 + 1
    wjson(os.path.join(d, "session.json"), {"partSizeInBytes": part, "parts": [
        {"partNumber": i, "uploadUrl": f"https://upload.example.test/{i}"} for i in (1, 2, 3)]})
    code = ("import sys, json; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]); import upload_parts as u; "
            "sent = []; u.put = lambda url, chunk: (sent.append((url, len(chunk))), 200)[1]; "
            "sys.argv = ['upload_parts.py', 'session.json', 'a.mp4']; u.main(); print('SENT', json.dumps(sent))")
    env = dict(os.environ, AGENCY_NOW="2026-11-02T09:00:00Z", CLIPPING_AGENCY_HOME=HOME[0])
    r = subprocess.run([PY, "-c", code, S], cwd=d, capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    ok(r.returncode == 0 and "Traceback" not in out, f"upload_parts.py success path: {out[-1200:]}")
    rec = rjson(os.path.join(d, "uploaded.json")) if os.path.isfile(os.path.join(d, "uploaded.json")) else {}
    ok(rec.get("a.mp4", {}).get("sha256") == lib.sha256(os.path.join(d, "a.mp4")) and rec["a.mp4"].get("bytes") == size
       and rec["a.mp4"].get("uploaded") == "2026-11-02T09:00:00Z", f"uploaded.json records the bytes sent: {rec}")
    ok("upload.example.test/3" in out and "All parts uploaded" in out, "every part sent, in order")


def main():
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is needed")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("ffmpeg is needed for the tests")
    unit_tests()
    d = tempfile.mkdtemp(prefix="agency-test-")
    HOME[0] = os.path.join(d, "user-home")
    ws = os.path.join(d, "agency")
    os.makedirs(os.path.join(ws, "clients"))
    try:
        make_source(d)
        run("check_setup.py", cwd=d, expect=None)
        setup_clients(ws, d)
        source_tests(ws, d)
        own = NO_CUT
        bp, edir = build_batch(ws, d, "alpha", "ep-41", own)
        if not NO_CUT:
            cut_tests(ws, bp, edir)
        check_tests(ws, bp, edir)
        fp = approve_tests(ws, bp, edir, d)
        schedule_tests(ws, bp, edir, d, fp)
        beta_tests(ws, d, edir)
        status_report_tests(ws, d)
        damaged_tests(ws, d, bp)
        upload_tests(d)
        offboard_tests(ws, bp)
        stray = [os.path.join(r, f) for r, ds, fs in os.walk(SKILL) for f in fs + ds if f == "__pycache__" or f.endswith(".pyc")]
        ok(not stray, f"scripts wrote into the skill folder: {stray}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{checks} checks, {len(failed)} failed" + (" (make_clips.py skipped: no Pillow)" if NO_CUT else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
