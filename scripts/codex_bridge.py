#!/usr/bin/env python3
"""Opt-in bridges from the living-picture skill to OpenAI Codex. Standard library only.

The skill's tools never upload anything. These two commands exist for when the picture's
owner wants Codex involved; nothing is sent unless they say so.

  review FRAME... [--checklist FILE] [--claims TEXT|@FILE] [--model M] [--out DIR]
                  [--user-config] [--consent-upload]
      An independent, adversarial review of rendered frames by Codex (codex exec), against
      the skill's checklist, answered as JSON. Without --consent-upload it only stages the
      files in --out and prints the command, what it would send and to whom; exit 0. When
      sent, Codex runs in a fresh /tmp folder holding only copies of those files.
  plate [--job DIR] [--work DIR] [--layer ID]
      Prints how an opt-in plate repair with the imagegen skill bundled with Codex works,
      what it sends, and how the result is checked. Sends nothing.

Codex is found through $LIVING_PICTURE_CODEX, then PATH, then inside ChatGPT.app.
"""
import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HOME = os.path.expanduser("~")
APP_CODEX = ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
             os.path.join(HOME, "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"))
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin")  # GUI-launched shells often lack these


def _j(o):
    return json.dumps(o, ensure_ascii=False, indent=1)


def die(msg, code=2):
    sys.stderr.write("codex_bridge: %s\n" % msg)
    sys.exit(code)


def tilde(p):
    return "~" + p[len(HOME):] if p == HOME or p.startswith(HOME + os.sep) else p


def find_codex():
    """The codex binary, or None: $LIVING_PICTURE_CODEX, PATH (plus Homebrew), then the copy inside ChatGPT.app."""
    path = os.pathsep.join([os.environ.get("PATH", "")] + list(EXTRA_PATH))
    env = os.environ.get("LIVING_PICTURE_CODEX")
    if env:
        return shutil.which(env, path=path) or (env if os.access(env, os.X_OK) else None)
    found = shutil.which("codex", path=path)
    if found:
        return found
    return next((p for p in APP_CODEX if os.access(p, os.X_OK)), None)


def codex_version(codex):
    """`codex --version`, run with a throwaway CODEX_HOME. Even --version writes helper links under $CODEX_HOME/tmp
    (making the folder if needed), and a check must neither change the person's Codex setup nor make it look used."""
    home = tempfile.mkdtemp(prefix="lp-codex-home-")
    try:
        r = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, CODEX_HOME=home))
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(home, ignore_errors=True)


def section(md, heading):
    """The text of one '## heading' section of a Markdown file, or ''."""
    m = re.search(r"^##\s+%s\s*$(.*?)(?=^##\s|\Z)" % re.escape(heading), md, re.M | re.S)
    return m.group(1).strip() if m else ""


# Strict structured-output shape: every key required, nothing extra.
VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "summary", "issues", "not_checked"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "frame", "where", "what", "likely_cause", "checklist_item"],
            "properties": {
                "severity": {"type": "string", "enum": ["blocking", "major", "minor"]},
                "frame": {"type": "string"},
                "where": {"type": "string"},
                "what": {"type": "string"},
                "likely_cause": {"type": "string"},
                "checklist_item": {"type": "string"}}}},
        "not_checked": {"type": "array", "items": {"type": "string"}}}}

PROMPT = """You are an independent, adversarial reviewer of a "living picture": a still painting that another agent animated for a web page by cutting it into depth layers and moving them in WebGL. You did not make it and owe it nothing. Your job is to find what is wrong; a pass needs evidence in the frames.

Use only the {n} attached images and this message. Do not run commands and do not open, list or read any file: everything you may use is here.

Attached frames, in order:
{frames}
(Frames from the capture tool carry their time in seconds after "_t". A "sheet" is a contact sheet of many frames; a "motion" map shows where and how much things moved.)

Claims by the author, to test rather than trust:
{claims}

Judge against the skill's own checklist below (written in Chinese; apply it as written). Look in particular for:
- holes, an old outline left in place, or old background travelling with a moved part, at every extreme;
- things standing on something that slide on it; parts of one creature or object drifting apart;
- text, signature, frame or picture edges that move or soften;
- marks that are not the painting's own medium: smears, stretched or thickened strokes, blur, new texture;
- released pieces (petals, snow, leaves) that carry their old ground, fringes, or visible repeats;
- whether the leading motion can be read at the size shown.

Stills cannot show rhythm, speed, loop seams, flicker or continuous playback: list those under not_checked and never pass them. Say so when something is ambiguous at this size.

Answer only with JSON matching the given schema:
- verdict: "fail" if there is any blocking or major issue; "pass" only if every checklist row that stills can show was looked for and nothing above minor was found; otherwise "uncertain".
- issues: severity (blocking = breaks a bottom line of the table; major = a clearly visible defect, a missing necessary response, or an unreadable leading motion; minor = small or arguable), frame (file name), where (the region in words plus an approximate pixel box x0,y0,x1,y1 in that frame), what you see, likely_cause (cutting/selection, fill/reveal, depth/camera, motion model, or runtime/fallback), checklist_item (the row or check it breaks).
- not_checked: what these frames cannot show.

=== SKILL.md: bottom lines before delivery ===
{bottom}

=== checklist: {checklist_name} ===
{checklist}
"""


_HOME = r"(?:~|\$HOME|\$\{HOME\})"
# Pairs that quote a whole path, spaces and all: ASCII and CJK quotes, angle brackets.
_PAIRS = (('"', '"'), ("'", "'"), ("`", "`"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ("《", "》"), ("<", ">"))
_QUOTED = re.compile("|".join(r"%s(%s?/[^%s\n]+)%s" % (re.escape(o), _HOME, re.escape(c), re.escape(c)) for o, c in _PAIRS))
_CAND = re.compile(_HOME + r"?/(?=[^\s/'\"`<>|])")
# Top folders of macOS and Linux: after a letter of another script (Chinese runs words and paths together) a path
# counts only when it starts with one of these, so "前景/中景/背景" stays text.
_ROOTS = {"Users", "Volumes", "private", "tmp", "var", "home", "Applications", "Library", "System", "opt", "usr", "mnt",
          "media", "srv", "root", "etc", "Network"}
_TRAIL = ".,;:!?)]}"


def _stop(c):
    """Whether c ends an unquoted path: white space, quotes, < > |, or punctuation of another script (，。：（）「」…)."""
    return c.isspace() or c in "'\"`<>|" or (ord(c) > 127 and unicodedata.category(c)[0] in "PSZ")


def _run_end(text, i):
    """Where the unquoted path starting at i ends; a backslash-escaped space belongs to it."""
    while i < len(text):
        if text[i] == "\\" and text[i + 1:i + 2] == " ":
            i += 2
        elif _stop(text[i]):
            break
        else:
            i += 1
    return i


def _local(p):
    """A path as this Mac would read it: ~ or $HOME expanded, escaped spaces unescaped."""
    return re.sub(r"^%s(?=/)" % _HOME, lambda _: HOME, p.replace("\\ ", " "))


def _name(path):
    """A path's file name, or [path] for a folder; trailing punctuation stays."""
    core = path.rstrip(_TRAIL)
    last = core.rstrip("/").rsplit("/", 1)[-1]
    return (last if re.search(r"\.[A-Za-z0-9]{1,5}$", last) and not core.endswith("/") else "[path]") + path[len(core):]


def _is_path(path):
    return path.count("/") >= 2 or bool(re.match(_HOME + "/", path))


def scrub(text):
    """Absolute paths reduced to their file name, or [path] for a folder: Codex only sees the attached frames, and
    folder names tell whose machine and which project. Returns (text, how many were reduced)."""
    n = 0

    def quoted(m):
        nonlocal n
        i = next(k for k, g in enumerate(m.groups(), 1) if g is not None)
        if not _is_path(m.group(i)):
            return m.group(0)
        n += 1
        whole, a, b = m.group(0), m.start(i) - m.start(0), m.end(i) - m.start(0)
        return whole[:a] + _name(m.group(i)) + whole[b:]
    text = _QUOTED.sub(quoted, re.sub(r"file://(?=/)", "", text))
    out, pos = [], 0
    for m in _CAND.finditer(text):
        s = m.start()
        if s < pos:
            continue
        prev = text[s - 1] if s else " "
        if prev.isascii() and (prev.isalnum() or prev in "._-/\\~$}"):
            continue                                     # inside a word or a relative path, or a URL's //
        e = _run_end(text, s)
        if prev.isalnum() and m.group(0) == "/" and text[m.end():e].split("/")[0] not in _ROOTS:
            continue                                     # right after a Chinese word: only a path from a top folder
        # In Chinese claims a file name can run straight into the next words ("f_t03.png眨眼正常"): it ends at its
        # extension.
        last = text.rfind("/", s, e) + 1
        tail = re.search(r"\.[A-Za-z0-9]{1,5}(?=[^\x00-\x7f])", text[last:e])
        if tail:
            e = last + tail.end()
        # Unquoted, a space may belong to the path ("Application Support"): take it only while the longer path still
        # names a folder or file on this Mac, so the words after a path stay.
        while not tail and e < len(text) and text[e] == " " and not _stop(text[e + 1:e + 2] or " "):
            k = _run_end(text, e + 1)
            cand, cut = text[s:k], text[s:k].rfind("/")
            if not ((cut > e - s and os.path.isdir(_local(cand[:cut]))) or os.path.exists(_local(cand.rstrip(_TRAIL)))):
                break
            e = k
        if _is_path(text[s:e]):
            out += [text[pos:s], _name(text[s:e])]
            n += 1
            pos = e
    return "".join(out) + text[pos:], n


def _users():
    names = {os.path.basename(HOME.rstrip("/"))}
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    return {u for u in names if u and len(u) >= 3}


def private_left(text):
    """What still looks like it names this machine or its user: folder roots, the home folder, the user name
    (also inside a flattened path such as -Users-<name>-Library)."""
    found = [f for f in ("/Users/", "/Volumes/", "/private/", "/tmp/", "/home/", "/var/folders/", "-Users-") if f in text]
    found += sorted({p for p in (HOME, os.path.realpath(HOME)) if p in text})
    found += sorted(u for u in _users() if re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(u), text, re.I))
    return found


RUN_DIR = "/tmp/lp-codex-XXXXXXXX"  # how the run folder looks; the real one is made only when sending


def run_folder():
    """A fresh private folder for the run, like RUN_DIR. Codex sends its working folder's path with every request,
    so the path must name no job, project or person; $TMPDIR is only the fallback, because agent sandboxes often put
    it under the person's home or project folder."""
    for base in ("/tmp", None):
        try:
            return tempfile.mkdtemp(prefix="lp-codex-", dir=base)
        except OSError:
            continue
    die("cannot make a temporary folder for the run")


def stage(out, frames):
    """Copy the frames into out/frames/ under plain names; exactly these copies are attached.

    codex splits --image values on commas, and file names should not carry the person's folder names.
    """
    d = os.path.join(out, "frames")
    os.makedirs(d, exist_ok=True)
    listing = os.path.join(d, ".staged")
    if os.path.exists(listing):  # remove only what an earlier run staged; nothing else is touched
        with open(listing, encoding="utf-8") as f:
            for old in f.read().split("\n"):
                p = os.path.join(d, old)
                if old and os.path.basename(old) == old and os.path.isfile(p):
                    os.remove(p)
    staged = []
    for i, f in enumerate(frames):
        name = "%02d-%s" % (i + 1, re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(f)))
        shutil.copyfile(f, os.path.join(d, name))
        staged.append("frames/" + name)
    with open(listing, "w", encoding="utf-8") as f:
        f.write("\n".join(os.path.basename(s) for s in staged) + "\n")
    return staged


def cmd_review(a):
    frames = [os.path.abspath(os.path.expanduser(f)) for f in a.frames]
    for f in frames:
        if not os.path.isfile(f):
            die("no such frame: %s" % f)
        if os.path.splitext(f)[1].lower() not in IMAGE_EXT:
            die("not an image codex can attach (%s): %s" % (", ".join(IMAGE_EXT), f))
    checklist = os.path.abspath(os.path.expanduser(a.checklist or os.path.join(ROOT, "references", "checking.md")))
    if not os.path.isfile(checklist):
        die("no such checklist: %s" % checklist)
    claims = (a.claims or "").strip()
    if claims.startswith("@"):
        try:
            with open(os.path.expanduser(claims[1:]), encoding="utf-8") as f:
                claims = f.read().strip()
        except (OSError, ValueError) as e:
            die("cannot read the claims file %s: %s" % (claims[1:], getattr(e, "strerror", None) or e))
    claims, reduced = scrub(claims)
    out = os.path.abspath(os.path.expanduser(a.out or os.path.join(os.path.dirname(frames[0]), "codex-review")))
    os.makedirs(out, exist_ok=True)
    staged = stage(out, frames)

    with open(checklist, encoding="utf-8") as f:
        checklist_text = f.read().strip()
    own_checklist = not os.path.realpath(checklist).startswith(ROOT + os.sep)
    if own_checklist:  # someone's own checklist may name their folders
        checklist_text, n = scrub(checklist_text)
        reduced += n
    try:
        with open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8") as f:
            bottom = section(f.read(), "交付前逐项看")
    except OSError:
        bottom = ""
    prompt = PROMPT.format(n=len(staged), frames="\n".join("%d. %s" % (i + 1, os.path.basename(s)) for i, s in enumerate(staged)),
                           claims=claims or "(none given)", bottom=bottom or "(not found)",
                           checklist_name=os.path.basename(checklist), checklist=checklist_text)
    with open(os.path.join(out, "prompt.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    with open(os.path.join(out, "verdict.schema.json"), "w", encoding="utf-8") as f:
        json.dump(VERDICT_SCHEMA, f, indent=1)

    codex = find_codex()
    # Codex runs in a fresh /tmp/lp-codex-… folder that holds only copies of these files: it sends its working folder
    # with each request, and the job folder would name the person's folders and sit one `..` from the original.
    # Read-only sandbox, no local session record, the prompt on stdin (a trailing prompt would be swallowed by
    # --image, which takes several values), the answer in a file that is copied back into `out`.
    cmd = [codex or "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
           "--color", "never", "--cd", RUN_DIR, "--output-schema", "verdict.schema.json",
           "--output-last-message", "verdict.json"]
    if not a.user_config:
        cmd.append("--ignore-user-config")  # no personal MCP servers or profiles; skills and AGENTS.md still load
    if a.model:
        cmd += ["--model", a.model]
    for s in staged:
        cmd += ["--image", s]
    files = [os.path.join(out, n) for n in ("frames", "prompt.md", "verdict.schema.json")]
    shell = ('d="$(mktemp -d %s)" && cp -R %s "$d"/ && (cd "$d" && %s < prompt.md); cp "$d/verdict.json" %s; rm -rf "$d"') % (
        RUN_DIR, " ".join(shlex.quote(p) for p in files), " ".join('"$d"' if c == RUN_DIR else shlex.quote(c) for c in cmd),
        shlex.quote(os.path.join(out, "verdict.json")))
    sizes = [os.path.getsize(os.path.join(out, s)) for s in staged]
    warnings = []
    if len(staged) > 12 or sum(sizes) > 20e6:
        warnings.append("%d frames, %.1f MB: a contact sheet (living-picture sheet) plus a few key frames sends less" % (len(staged), sum(sizes) / 1e6))
    if not codex:
        warnings.append("codex not found on PATH or in ChatGPT.app; set LIVING_PICTURE_CODEX to run it")
    if reduced:
        warnings.append("%d absolute path(s) in what you passed were reduced to a file name or [path]; read prompt.md for "
                        "what is left" % reduced)
    # What the person passed travels: the claims, their own checklist, the frames' file names.
    left = private_left("\n".join([claims, checklist_text if own_checklist else ""] + [os.path.basename(s) for s in staged]))
    if left:
        warnings.append("prompt.md or the frame names still contain what can name this machine or you (%s): edit the "
                        "claims, or rename the frames, before sending" % ", ".join(left))
    receiver = ("OpenAI, under the account Codex is signed in with (your ChatGPT account, or an OpenAI API key if Codex "
                "was set up with one)" if not a.user_config else
                "whatever model provider your Codex config selects (OpenAI unless you changed it), under that sign-in")
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    codex_adds = ["its sandbox settings and its working folder: a fresh /tmp/lp-codex-… folder holding only copies of "
                  "these files",
                  "the skills installed for Codex (~/.agents/skills, %s/skills and its bundled ones) with their names, "
                  "descriptions and file paths, which show your home folder, your user name and what you installed"
                  % tilde(codex_home),
                  "your global instructions, %s/AGENTS.md (or AGENTS.override.md), if you have them" % tilde(codex_home)]
    notice = [
        "Nothing has been sent." if not a.consent_upload else "Sending now, as agreed:",
        "Running it sends to %s:" % receiver,
        "  - %d frame(s), %.0f KB: the copies in %s (their file names travel, their folders do not)"
        % (len(staged), sum(sizes) / 1e3, os.path.join(out, "frames")),
        "  - prompt.md: these instructions, the skill's checklist%s%s" % (
            " and the claims you gave" if claims else "",
            " (absolute paths reduced to file names)" if reduced else ""),
        "  - what Codex itself adds to every request (--ignore-user-config turns none of it off):"] + [
        "      " + c for c in codex_adds] + [
        "Codex runs read-only: its commands cannot write files or reach the network. That sandbox does not stop it from",
        "reading files your user can read; the prompt tells it to read nothing and use only the attached frames.",
        "--ephemeral keeps no local session record; what OpenAI retains follows that account's data controls.",
        "Only run it with the picture owner's agreement: the frames show their painting.",
    ]
    info = {"mode": "sent" if a.consent_upload else "dry-run: nothing sent",
            "codex": codex, "codex_version": codex_version(codex) if codex else None,
            "cwd": "a fresh %s folder with copies of frames/, prompt.md and verdict.schema.json, removed afterwards" % RUN_DIR,
            "command": cmd, "stdin": "prompt.md", "shell": shell,
            "would_send": {"images": [{"file": s, "bytes": n} for s, n in zip(staged, sizes)],
                           "text": {"file": "prompt.md", "bytes": len(prompt.encode("utf-8"))},
                           "added_by_codex": codex_adds},
            "receiver": receiver, "verdict_file": os.path.join(out, "verdict.json"), "warnings": warnings}
    sys.stderr.write("\n".join(["codex review: " + notice[0]] + notice[1:]) + "\n")
    if not a.consent_upload:
        info["to_send"] = "after the owner agrees: the same review with --consent-upload, or the shell line, which does the same by hand"
        print(_j(info))
        return 0
    if not codex:
        die("codex not found; set LIVING_PICTURE_CODEX", 2)
    verdict_path = os.path.join(out, "verdict.json")
    if os.path.exists(verdict_path):
        os.remove(verdict_path)
    run_dir = run_folder()
    try:
        os.makedirs(os.path.join(run_dir, "frames"))
        for n in staged + ["prompt.md", "verdict.schema.json"]:
            shutil.copyfile(os.path.join(out, n), os.path.join(run_dir, n))
        cmd = [run_dir if c == RUN_DIR else c for c in cmd]
        info.update({"cwd": run_dir, "command": cmd})
        with open(os.path.join(run_dir, "prompt.md"), "rb") as stdin:
            r = subprocess.run(cmd, cwd=run_dir, stdin=stdin, stdout=sys.stderr)  # its progress on stderr, our JSON on stdout
        if os.path.exists(os.path.join(run_dir, "verdict.json")):
            shutil.copyfile(os.path.join(run_dir, "verdict.json"), verdict_path)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    verdict = None
    if os.path.exists(verdict_path):
        with open(verdict_path, encoding="utf-8") as f:
            text = f.read()
        try:
            verdict = json.loads(text)
        except ValueError:
            verdict = {"unparsed": text}
    info.update({"exit": r.returncode, "verdict": verdict})
    print(_j(info))
    return 0 if r.returncode == 0 and isinstance(verdict, dict) and "verdict" in verdict else 1


def cmd_plate(a):
    codex = find_codex()
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    imagegen = os.path.join(codex_home, "skills", ".system", "imagegen", "SKILL.md")
    res = {
        "sends": "nothing: this prints guidance only",
        "when": ("A layer has to uncover a large area (its joint reveal band: own motion plus parent plus camera) whose hidden "
                 "surface was never painted, such as a face behind a hand or the far bank behind a boat "
                 "(references/cutting.md section 4), and the local fills cannot reconstruct it."),
        "try_first_locally": [
            "another part of the picture or an existing author layer that shows the hidden surface",
            "fill method surfaces with the right surface map and 'as', or regions with donors on the correct surface; "
            "rebuild long lines (a horizon, a sill) on their own surface",
            "change the expression so less is uncovered, for a reason in the picture (SKILL.md rule 8: change the "
            "expression before giving anything up); never just shrink the motion to hide a bad plate",
        ],
        "imagegen_can": {
            "source": "%s (as shipped with Codex 0.169; re-read it if your Codex is newer)" % imagegen,
            "installed": os.path.exists(imagegen),
            "built_in_tool": ("Default mode, inside a Codex session, under the Codex sign-in, no API key. It edits images "
                              "that are visible in the conversation (a local file is first shown with view_image). It takes "
                              "no mask: the change is described in words with invariants ('change only X; keep Y "
                              "unchanged'), and the skill says not to promise arbitrary file-path editing with it. Results "
                              "are saved under $CODEX_HOME/generated_images/ by default."),
            "mask": ("Masks, exact file paths and output size exist only in the skill's fallback CLI "
                     "(scripts/image_gen.py edit, gpt-image-2 by default). It needs OPENAI_API_KEY (an OpenAI API "
                     "platform key, billed to that API account), and the skill uses it only when the person asks for it."),
            "size": ("The result is not guaranteed to have the working picture's size or pixel grid (CLI gpt-image-2 "
                     "sizes need both edges in multiples of 16, at most 3840 px, ratio at most 3:1)."),
        },
        "leaves_the_machine": [
            "every image shown to Codex for this, i.e. a crop of the person's painting, and the prompt: to OpenAI under the "
            "Codex sign-in (normally the ChatGPT account) in built-in mode, or under the API key's organisation in CLI mode, "
            "which also uploads the mask",
            "Codex's own session context; a Codex session keeps a local record under $CODEX_HOME unless run with "
            "--ephemeral, and generated images land in $CODEX_HOME/generated_images/",
            "so: ask the picture's owner first, and name what will be sent",
        ],
        "steps": [
            "Find the band that is really uncovered, from the scene's motion (local + parent + camera); only that needs "
            "paint. Look at work/review/plate-after-<id>.jpg with living-picture look --box.",
            "Make the input locally: crop work/picture.png around the band with a margin of painted context, paint the "
            "hole one flat colour that does not occur in the painting (pure magenta, say) so a maskless edit knows where "
            "to work, and keep the crop's aspect within what the model accepts.",
            "In Codex, with the person's agreement, ask through $imagegen in the skill's own prompt shape: 'Use case: "
            "precise-object-edit. Input images: Image 1: edit target. Primary request: repaint only the magenta area so "
            "the surrounding <medium> continues where <the object> was: <what continues>. Constraints: change only the "
            "magenta area; keep every other pixel unchanged; same medium, brush size, stroke direction, paper texture and "
            "palette as the painting around it; no new objects; no text.'",
            "Bring it back: copy the chosen result from $CODEX_HOME/generated_images/ into the job (plates/), scale it "
            "back to the crop's exact size, paste it into a full-size copy of work/picture.png at the crop's place, save "
            "an opaque PNG at the working size (a few lines of Pillow).",
            "Use it as a guide or as the plate (see rule_8), with \"fill\": {\"method\": \"plate\", \"image\": "
            "\"plates/<id>.png\"} on that layer: cut consumes a plate only inside this peel's hole and keeps the observed "
            "picture elsewhere (references/reveal-contract.md); a file is not trusted because it was supplied.",
        ],
        "check": [
            "native size: living-picture look --box and living-picture inspect <work> --layer <id> on every edge that is "
            "uncovered; compare stroke width and direction, edge softness, pigment, paper grain and value range with the "
            "painting's own marks beside the hole",
            "in motion: capture the joint extremes (object, parent and camera together) at the target display size: no "
            "seam, no colour step, no invented detail",
        ],
        "rule_8": ("SKILL.md rule 8 never concedes marks that do not come from the painting, and a generated repaint is new "
                   "paint by definition. So by default use it as a guide: read from it how the hidden surface continues, "
                   "then build the shipped plate from the painting's own marks (surfaces with 'as', regions, patch from the "
                   "right surface) and confirm that no generated pixel is left in the delivered layers. Shipping the "
                   "generated paint itself departs from that rule: only on the owner's explicit decision, kept to the "
                   "uncovered band, and recorded in notes.md (资产处理, 规则 8 取舍: what was generated, where, the prompt, "
                   "who approved)."),
        "codex": codex,
    }
    if a.job or a.work:
        work = os.path.abspath(os.path.expanduser(a.work or os.path.join(a.job, "work")))
        p = {"picture": os.path.join(work, "picture.png")}
        if a.layer:
            p.update({"layer": os.path.join(work, "layers", a.layer + ".png"),
                      "plate_after": os.path.join(work, "review", "plate-after-%s.jpg" % a.layer),
                      "plate_to_write": os.path.join(os.path.dirname(work), "plates", a.layer + ".png")})
        res["paths"] = {k: {"path": v, "exists": os.path.exists(v)} for k, v in p.items()}
    print(_j(res))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("review", help="independent review of rendered frames by Codex (dry run unless --consent-upload)")
    p.add_argument("frames", nargs="+", help="rendered frames or sheets (png, jpg, webp)")
    p.add_argument("--checklist", help="default: references/checking.md of this skill")
    p.add_argument("--claims", help="what the author claims, to be tested; text, or @file")
    p.add_argument("--model", help="Codex model (default: Codex's own)")
    p.add_argument("--out", help="folder for the staged copies, prompt.md and verdict.json (default: codex-review/ next to the first frame)")
    p.add_argument("--user-config", action="store_true", help="load your Codex config.toml (default: ignored, for a minimal run)")
    p.add_argument("--consent-upload", action="store_true",
                   help="really run it: the frames and the prompt go to OpenAI. Only with the picture owner's agreement")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("plate", help="guidance for an opt-in Codex imagegen plate repair; sends nothing")
    p.add_argument("--job", help="job folder, to print its paths")
    p.add_argument("--work", help="the cut's output folder (default: <job>/work)")
    p.add_argument("--layer", help="the layer whose hidden area needs paint")
    p.set_defaults(fn=cmd_plate)
    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 1
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
