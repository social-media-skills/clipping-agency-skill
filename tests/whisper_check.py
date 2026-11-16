#!/usr/bin/env python3
"""A real transcription of one of the example clients' NASA clips with faster-whisper (tiny model), through
the skill's own local route: ffmpeg decodes the sound to 16 kHz samples and the model gets the
samples, never a file path (PyAV 19 broke decoding from a path). It also checks an exact phrase of the clip
("plan to guess"), so a word cut out of the example clip fails it.

    python3 tests/whisper_check.py        # needs ffmpeg and faster-whisper; downloads the tiny model once
"""
import json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "clipping-agency-skill")
clip = os.path.join(SKILL, "examples", "agency", "clients", "example-mission-talks", "episodes", "ep-007", "clips",
                    "clip-01.mp4")
tmp = tempfile.mkdtemp(prefix="agency-whisper-")
try:
    shutil.copy(clip, os.path.join(tmp, "clip.mp4"))
    r = subprocess.run([sys.executable, os.path.join(SKILL, "scripts", "transcribe.py"), "clip.mp4", "--engine", "local",
                        "--model", "tiny", "--out", "work"], cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    print(r.stdout, r.stderr[-1500:])
    if r.returncode != 0 or "Traceback" in r.stderr:
        sys.exit("FAILED: transcription did not run")
    data = json.load(open(os.path.join(tmp, "work", "transcript.json"), encoding="utf-8"))
    words = data["words"]
    text = " ".join(w["w"] for w in words).lower()
    print(len(words), "words:", text[:200])
    problems = []
    must = ["prepared", "training"]
    missing = [m for m in must if m not in text]
    if missing:
        problems.append(f"missing words {missing}")
    plain = " ".join("".join(ch for ch in t if ch.isalnum() or ch == "'") for t in text.split())
    if "plan to guess" not in plain:   # an exact phrase: a word cut out of the clip shows up here
        problems.append("the phrase 'plan to guess' isn't heard whole")
    if len(words) < 25:
        problems.append(f"only {len(words)} words")
    if not all(0 <= w["start"] <= w["end"] for w in words) or words[-1]["end"] > 40:
        problems.append("word times out of range")
    if not os.path.isfile(os.path.join(tmp, "work", "peaks.txt")):
        problems.append("no peaks.txt")
    if problems:
        sys.exit("FAILED: " + "; ".join(problems))
    print("whisper check passed")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
