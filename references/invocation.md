# Invocation: skill, CLI, batches and Codex

How to call this skill from Claude Code, from Codex, from a terminal and over many pictures, and what leaves the machine when Codex is involved.

## Three layers

| Layer | What it is | Who uses it |
|---|---|---|
| Skill: judgment | `SKILL.md` and `references/`. An agent reads the picture and decides what moves, what stays and whether it is good. No script can take this over. | Claude Code and Codex, from one shared copy |
| CLI: tools | `bin/living-picture`: one command for every tool (`picture.py`, `capture.mjs`, the macOS helpers), JSON on stdout. It sees, measures, cuts and renders. It does not decide. | any agent, a terminal, Shortcuts |
| Orchestration | `workflows/living-picture-batch.js` for several pictures with an independent review; `scripts/codex_bridge.py` for an opt-in second opinion from Codex | Claude Code workflows; the person, on request |

For one picture in a conversation, the skill alone is enough. The CLI gives the same tools to any agent or a terminal without knowing where the skill is installed. The workflow runs many pictures at once and adds a reviewer who did not do the work. Neither one changes how the work is judged.

## Install once for Claude Code and Codex

Keep one canonical folder and link it everywhere:

```sh
scripts/install.sh                     # dry run: prints the plan, changes nothing
scripts/install.sh --apply             # make the links (Claude Code, and ~/.agents/skills for Codex)
scripts/install.sh --apply --with-cli  # also ~/.local/bin/living-picture
scripts/install.sh --uninstall         # plan removing every link that points here (add --apply)
scripts/install.sh --only agents,cli   # exactly these targets: claude, agents, codex, cli
```

| Target | Read by | Linked | Invoke |
|---|---|---|---|
| `~/.claude/skills/living-picture` (`$CLAUDE_CONFIG_DIR`) | Claude Code; it follows symlinked skill folders and picks up changes live | by default, when `~/.claude` exists | `/living-picture`, or by description |
| `~/.agents/skills/living-picture` | Codex: its documented user skills folder (symlinks followed); also the cross-agent convention | by default, when `~/.agents` exists or Codex has been used here | `$living-picture` or `/skills` in Codex |
| `$CODEX_HOME/skills/living-picture` (`~/.codex`) | Codex as well: the bundled 0.169 build reads it too, and its skill-installer writes there | only with `--only codex`, for an older Codex that does not read `~/.agents/skills` | same |
| `~/.local/bin/living-picture` | your shell | with `--with-cli` | `living-picture ...` |

One Codex link is enough. Codex's docs say that two skills with the same name are not merged and both can appear in its selectors. Whether it folds two links to one folder into one entry was not tested here (only `codex --help` and `--version` were run). So the installer makes one link, and both it and `doctor` warn when both folders lead here.

"Codex has been used here" means `$CODEX_HOME` holds `skills/`, `sessions/` or `config.toml`. A bare `~/.codex` is not enough, because even `codex --version` creates one. The script never replaces a real folder, a file, or a symlink pointing elsewhere: it reports them and skips them. `--only` without a list, or with a name that is not a target, stops it with exit 2 instead of doing nothing. If the canonical copy already sits in one of the targets (as `~/.claude/skills/living-picture` does here), that target is reported as "this folder". Both agents read the same frontmatter (`name`, `description`, `compatibility`). These fields are all in the shared Agent Skills format, so one `SKILL.md` serves both. `living-picture doctor` shows each target's state. If an agent update ever drops a link, run the installer again.

Job folders never go inside the skill folder: every agent shares it.

## The CLI

```sh
living-picture --help                       # every command; each one takes --help
living-picture doctor                       # what this Mac has; exit 1 if something required is missing
living-picture new ~/Downloads/painting.jpg --job ~/LivingPictures/painting
living-picture look JOB/work/picture.png --box 400 600 520 700
living-picture cut JOB/plan.json
living-picture bundle JOB/scene.json --out JOB/out
living-picture capture JOB/out/index.html --out JOB/check --every 0.5 --duration 12
living-picture paths                        # where SKILL.md, references, workflow, installer are (JSON)
```

- Subcommands of `picture.py` pass through unchanged: same options, same JSON. The list is read from `picture.py` itself, so a new tool such as `vision` appears as soon as it exists. `capture` runs `capture.mjs`.
- Python. `bin/living-picture` begins as a small `sh` launcher that picks the Python for the CLI itself. It tries `$LIVING_PICTURE_PYTHON`, then `python3` on PATH unless that is Apple's `/usr/bin/python3`, then Homebrew, conda/miniforge and python.org. Apple's `/usr/bin/python3` comes last, and only when `xcode-select -p` finds the developer tools: without them that stub opens the Command Line Tools installer instead of running. If none is usable, it prints what to install and exits 2. The CLI then runs `picture.py` with `$LIVING_PICTURE_PYTHON` if set, otherwise the interpreter running it if that has numpy, scipy and Pillow, otherwise the first of the same candidates that has them (the stub is never probed). Macs often have several Pythons and only one has the packages. `doctor` says which interpreter it chose and why.
- Child processes get `/opt/homebrew/bin` and `/usr/local/bin` appended to PATH. Apps started from the Dock, Shortcuts or Finder get a bare PATH, and `capture` still needs `node` and `ffmpeg`. Other overrides: `LIVING_PICTURE_NODE`, `CHROME_PATH`, `LIVING_PICTURE_CODEX`.
- `new <image> [--job DIR] [--max N] [--force] [--reprepare] [--keep-watermark] [--as-is]` runs three steps in this order, then `prepare` and `look`, and writes `notes.md` with the headings that SKILL.md's rules and handoff list ask for:
  1. the watermark step, on the picture as received (a platform's mark belongs to the posted image's corner, not to the work);
  2. the photo correction, on the watermark repair `clean.png` when there is one, otherwise on the picture;
  3. `prepare` from whatever the last step wrote: `rectified.png`, `clean.png` or the picture itself.

  `notes.md` also records the original's path, sha256 and size, the working size and a fingerprint of the tools. The default job folder is `./<picture name>`. The picture itself is never changed. The JSON's `prepared_from` names the file `work/picture.png` came from, and `scale_from_source` is relative to that file.
  - **Watermark.** It looks for a share-platform watermark (`picture.py dewatermark`: the logo pill in the lower right with the ID line right-aligned under it; both must be there) and prints one line while it searches. When one is found, the restored copy `clean.png` and its review sheet `clean-review.jpg` go into the job, the next step works from `clean.png`, and the JSON says `"watermark_removed": true` with a step to check the sheet. Whatever happens, `watermark.search` in the JSON says it (`removed`, `none found`, `kept`, `skipped` or `failed`), and `notes.md` gets a line under 资产处理. `--keep-watermark` skips the search.
  - Only pictures up to 2560 px on the longer side are searched. Share platforms deliver smaller pictures (the marked downloads tested were 928 to 1440 px), and the search runs at full resolution. Measured on an M2 Max: 4.7 s and 0.55 GB at 2.8 MP, 11 s at 2560 × 1707, 44 s and 1.6 GB at 12 MP, 100 s and 2.4 GB on a 24 MP photo. A larger picture goes on as received with `skipped`, and the JSON's next steps give the commands to search it by hand (below).
  - Only exit 4 of `dewatermark` means nothing was found. Any other ending (a crash, an out-of-memory kill, an unreadable file) gives `failed`, a warning with the tool's last words and the same line in `notes.md`, and the picture goes on as received.
  - **Photo correction.** Next it runs `picture.py rectify` (`references/photo.md`) and prints one line while it works. For a casual photo of the work (on a wall, a table or in hands, in a frame or mat, at an angle, under a warm lamp) rectify finds the work, crops it out, undoes perspective and lens bending, and evens out light and a colour cast. It does light and colour only against neutral evidence (a paper margin, mat, or near-neutral wall or table) and leaves them as photographed otherwise. It never sharpens or boosts. Its output keeps the work at its own detail in the photo, up to 4096 px on the longer side or the job's `--max` if that is larger. `prepare` then caps the working copy as usual.
    - **Exit 0** (something was corrected). `rectified.png` and its review sheet `rectified-review.jpg` go into the job, and `work/picture.png` is prepared from `rectified.png`. The JSON says `"photo_corrected": true`. Its `rectify` block holds rectify's report: `quad` (corners in the input's pixels, source, confidence), `corrected` (what was changed), `frame`, `lens`, `perspective`, `light`, `color`, `exposure`, `warnings`, `note`, and `alternatives` (other candidate boundaries, ready for `--corners`). Each rectify warning is also in the top-level `warnings`, prefixed `photo correction:`. The first next step is to check `rectified-review.jpg`. `notes.md` gets the quad, what was corrected (with tilt, aspect, k1, light variation before and after, colour gains, exposure), what was left as photographed and why, and every warning. It also records what `rectified.png` was corrected from, with the start of its sha256 (`rectified.png（sha256 …，由原图校正）` or `由 clean.png 校正`), which `--reprepare` relies on.
    - **Exit 4** (nothing to correct, or no boundary found with confidence). Nothing is written: `"photo_corrected": false`, `rectify.run` is `nothing to correct`, and its `note` says why. A flat scan or screenshot of the work ends here and goes through unchanged, apart from the watermark repair. The next steps then include a by-hand command, for when `look.jpg` shows a photo that rectify could not read (below).
    - **Any other ending** (a crash, an out-of-memory kill) gives `rectify.run: failed` and a warning with the tool's last words. The picture (or `clean.png`) is then prepared as it is.
    - `--as-is` skips the step (`rectify.run: skipped`), as `--keep-watermark` skips the watermark.
    - The last line under 资产处理 always ends with what the working copy was prepared from: `工作图由 rectified.png 准备`, `工作图由 clean.png 准备` or `工作图由原图准备`.
  - **What to look at.** After `new`, look at `rectified-review.jpg` first, at display size and zoomed in. Its three panels are the photo with the boundary found (magenta; a removed frame or mat in cyan), the corrected picture, and where and how much the light changed. Check:
    - the boundary sits on the work's own edge all round;
    - no frame, mat, wall or table is left, and nothing of the painting is cut off;
    - straight lines are straight;
    - the light map follows the lamp, not the painting;
    - the colours look like the painting, not like a white-balanced photo of a room.

    Then read the warnings. Glare, blur or shake, too few pixels, a steep angle and a cut-off work cannot be fixed afterwards: tell the person a reshoot would be better (`photo.md` lists what to do for each). Then look at `clean-review.jpg` if there is one, and at `look.jpg`.
  - To redo a repair or a correction, or to search a picture that was skipped, write the copy into the job yourself and re-prepare from it:

    ```sh
    living-picture dewatermark IMG --out JOB/clean.png [--pill X0 Y0 X1 Y1 --line X0 Y0 X1 Y1] [--mode fill|untint]
    # look at JOB/clean-review.jpg at display size and at 4x; if the job has a rectified.png, redo it from the repair:
    living-picture rectify SRC --out JOB/rectified.png [--corners "x,y;x,y;x,y;x,y"] [--frame keep|remove] [--light off] [--color off]
    # SRC is JOB/clean.png when the job has one, else IMG; look at JOB/rectified-review.jpg, then:
    living-picture new IMG --job JOB --reprepare
    ```

    `--corners` are the work's corners in SRC's pixels (after EXIF orientation), top-left, top-right, bottom-right, bottom-left. Read them off `living-picture look SRC --out JOB/photo-look.jpg` (with `--box` to zoom; without `--out`, `look` writes next to SRC), or take a candidate from the JSON's `rectify.alternatives`. For a work that runs off the photo, give the visible edges and put the missing corners on the photo's border. The real museum photo tested here was done this way: rectify found no boundary, and the corners from its one visible edge gave a level, cropped result. To drop a correction: `new IMG --job JOB --reprepare --as-is`.
  - It claims the folder before preparing anything, by creating `notes.md` exclusively, and moves the prepared files in only when every step has finished and `prepare` and `look` have succeeded. Two runs into one folder cannot both succeed: on a Mac, `Hero/` and `hero/` are one folder. A failed run removes what it made, and removes the folder too if it created it.
  - It never changes a folder that already holds a job (`notes.md` or `work/picture.png`). The refusal (exit 2) says whether that job is for the same picture (same sha256) or another one.
  - `--reprepare` redoes `work/picture.png` and `look.jpg` of an existing job, for example after the tools changed or after a copy was redone by hand. It works only for the picture that `notes.md` names, and by default with the job's own longer side as the cap (`--max` picks another).
    - Once files refer to the old pixel grid, it refuses a change of size and lists them (`plan.json`, `scene.json` and other JSON in the job, `work/layers`, `work/review`, `work/vision`, `out`): a different working size then needs a new job.
    - Before that (right after `new`, typically after redoing `rectified.png` with other corners), the new size is taken. It is reported in `size_changed`, with a warning that coordinates already noted in `notes.md` are in the old size, and a line under 工作尺寸.
    - If the pixels changed at the same size, it names the outputs to redo.
  - It never runs `dewatermark` or `rectify`. It repeats the job's choices by preparing from the job's own copies: `rectified.png` when there is one, else `clean.png`, else the picture as received. So a copy made by hand is used, not overwritten, and a choice made with `--as-is` or `--keep-watermark` stays made. `--as-is` leaves `rectified.png` out and renames it and its sheet to `rectified-unused.png` and `rectified-unused-review.jpg`. `--keep-watermark` does the same with `clean.png` (`clean-unused.png`). It refuses (exit 2, nothing changed) instead of silently switching to another source when:
    - `notes.md` says the working copy came from `rectified.png` and that file is gone (make it again, or pass `--as-is`);
    - it came from `clean.png`, directly or through `rectified.png`, and `clean.png` is gone when it would be needed (make it again, or pass `--keep-watermark`);
    - `rectified.png` was recorded as corrected from `clean.png` and `--keep-watermark` asks to leave the repair out;
    - `rectified.png` was recorded as corrected from the picture, but the job now has a `clean.png`, so the repair would be ignored.

    The record counts only while `rectified.png` still has the sha256 noted. For a copy redone by hand, the source is unknown: it is used as it is, recorded as `来源未记录`, and the JSON says `corrected_from: "not recorded (made by hand)"`.
  - It adds one dated line under 资产处理 in `notes.md` (what it prepared from, with the `rectified.png` record; whether the pixels or the size changed; what it renamed) and leaves the rest of the notes alone.
  - `--force` only allows a folder inside a git repository or inside the skill folder. It never overrides an existing job.
  - It warns when the folder is inside iCloud Drive or `~/Library/CloudStorage`, and when it is in Desktop or Documents, which iCloud may sync.
  - Time and memory, measured on an M2 Max with other jobs running (load 10-13): a flat picture of 0.5-1.8 MP takes 5-12 s in all, most of it the watermark search; a 12 MP photo with the watermark search skipped and the photo corrected takes 17 s. The 48 MP benchmark photo takes 18 s: prepare 4.5 s, rectify 8.9 s, prepare of the 4090 × 3219 result 2.4 s, look 0.5 s. The steps run one after another, so peak memory is rectify's own: up to 2.9 GB on 48 MP (2.3 GB polled during `new`).
  - Limits:
    - rectify on `clean.png` sees no EXIF. A platform download has none anyway, but proportions then come from vanishing points or an assumed 26 mm lens.
    - A work cut off by the photo can come out with transparent corners: `prepare` keeps them, so `work/picture.png` then has an alpha channel.
    - A `rectified.png` made by hand has no recorded source.
- It resolves its own real path, so a symlink (`~/.local/bin/living-picture`) works. Apart from the launcher, it uses only the Python standard library.
- `doctor` checks:
  - Python and its packages.
  - Node 22+. A `LIVING_PICTURE_NODE` that does not run is reported as missing, not as old.
  - The browser, picked exactly as `capture.mjs` picks it: the first that exists of `CHROME_PATH` and the list in its own code. A wrong `CHROME_PATH` is therefore a warning, not a failure.
  - ffmpeg, and the Xcode tools for the Swift helpers. It runs `xcode-select -p` before `xcrun`, so a Mac without the tools gets advice instead of an installer dialog.
  - Whether those helpers are compiled in `~/Library/Caches/living-picture/`.
  - Whether `sips`, `avconvert`, `qlmanage` and `shortcuts` are present.
  - The Codex CLI (PATH, then inside ChatGPT.app). It asks the version with a throwaway `CODEX_HOME`, because even `codex --version` writes into that folder.
  - Every install target.

  Output is text in a terminal and JSON otherwise (`--json`, `--text`).

## Batches

In Claude Code, run the workflow with the Workflow tool:

```
Workflow({ scriptPath: "/Users/<you>/.claude/skills/living-picture/workflows/living-picture-batch.js",
           args: { images: ["/abs/a.jpg", "/abs/b.png"], root: "/abs/LivingPictures",
                   notes: "calm; hero banner about 1200 px wide", concurrency: 4 } })
```

You can also copy the file into a project's `.claude/workflows/` and run it by name (`living-picture-batch`). Pass `args` as a JSON object, not as a string. Each picture goes through a pipeline, so a picture moves on as soon as its own previous stage is done:

1. **Make**: one agent reads SKILL.md and does the whole job in `root/<name>/`, starting with `living-picture new`. If that folder already holds a job, the agent continues it only when the sha256 in its `notes.md` matches the picture. Another picture's job makes it stop and report instead. It returns checkable claims: intent, leading dynamic, what moves, what stays and why, the checks it ran with evidence paths, and open issues.
2. **Review**: a fresh agent gets only the job folder and those claims. It writes its own reading of the picture before reading the claims. It renders its own evidence into `review/` and tests the claims against SKILL.md's table and `checking.md`, returning issues with severity, evidence path and cause.
3. **Fix**: runs for blocking or major issues, and for a `not deliverable` verdict even when the reviewer listed no such issue; then the fixer first finds out why, and the run log notes the mismatch. It fixes causes, re-renders, re-checks and returns the final state. The fixer checks its own work; there is no second independent review, and the run log says so. A picture whose review said `not deliverable` and that is not fixed is reported as `not deliverable (review), …`, never as `minor issues only`.

Job folder names never meet in one folder on a case-insensitive Mac volume. `Hero.png`, `hero.jpg` and `HERO.webp` become `Hero`, `hero-2` and `HERO-3`, a composed and a decomposed accent count as the same name, and accents are kept. A name made only of dots becomes `picture`. If a stage fails for one picture, the report marks it `failed` and still reports the others.

At most `concurrency` agents run at once (default 4), since each one starts Chrome and runs the cutting tools. Every prompt carries the privacy rules: no uploads, installs or downloads, no Codex bridges, and writes only inside the job folder. Each picture costs one full agent session, plus a reviewer and maybe a fixer.

Codex has no workflow runner. Run one Codex session per picture, each with its own job folder, in the Codex app or with `codex exec`. This has not been tested here.

## Opt-in Codex bridges, and what leaves the machine

The tools never upload anything. The agent you work in sees the pictures it looks at, as with any image you show it: Claude Code sends them to Anthropic and Codex to OpenAI. The bridges add Codex as a second recipient, so use them only when the picture's owner asks. The batch workflow forbids them.

**`living-picture codex review FRAME... [--claims TEXT|@FILE] [--model M] [--out DIR] [--consent-upload]`** asks Codex (`codex exec`) for an independent, adversarial review of rendered frames against `checking.md` and SKILL.md's bottom-line table. The answer is a JSON verdict (`pass`/`fail`/`uncertain`, issues with severity, frame, region, likely cause, checklist row, and what stills cannot show), with `--output-schema` enforcing the shape.

Without `--consent-upload` it only stages the files in `--out` (default `codex-review/` next to the first frame), prints the command, the byte counts and the recipient, and exits 0. When run, it copies the staged files into a fresh `/tmp/lp-codex-XXXXXXXX` folder and runs Codex there. It then copies `verdict.json` back into `--out` and deletes the folder. The dry run's `shell` line does the same by hand. A run sends:

- the frames named in the command, as numbered copies under their own file names (`01-f_t03.00.png`). Their folder names do not travel;
- `prompt.md`: the instructions, the checklist text and any claims you passed. Absolute paths in the claims (and in a checklist of your own) are reduced to the file name, or `[path]` for a folder:
  - A path in ASCII or Chinese quotes or in angle brackets is taken whole, spaces and all.
  - An unquoted path starts after a space or any punctuation, Chinese punctuation included (`证据：/Volumes/…`). It ends at a space, a quote or Chinese punctuation. `\ ` belongs to it, and a file name that runs straight into Chinese words ends at its extension (`f_t03.00.png眨眼正常`).
  - Right after a Chinese word, only a path from a top folder (`/Users`, `/Volumes`, `/private`, `/tmp` and the like) counts, so `前景/中景/背景` stays text.
  - An unquoted path with spaces is followed only as far as it exists on this Mac, so the rest of a path from elsewhere can survive. A folder path that runs into Chinese words with no punctuation between takes those words with it.
  - The output says how many paths were reduced. It warns when the claims, your own checklist or the frame names still contain `/Users/`, `/Volumes/`, `/private/`, `/tmp/`, a flattened `-Users-…` path, your home folder or your user name. Other words are sent as written, so read `prompt.md` before agreeing;
- what Codex adds to every request by itself. That is its sandbox settings and its working folder: the `/tmp/lp-codex-…` copy, which is not next to the job. It is also the list of skills installed for Codex, with their names, descriptions and file paths. OpenAI's skills documentation says the initial list includes each skill's file path, and those paths show your home folder, your user name and which skills you installed. Finally, your global instructions `$CODEX_HOME/AGENTS.md` (or `AGENTS.override.md`) if you have them. `--ignore-user-config` turns none of this off. Codex's documentation describes switching skills off with `[[skills.config]]` entries (`enabled = false`) and `[skills.bundled] enabled = false` in `config.toml`. The bridge sets neither, because a consented run was never tested here.

It all goes to OpenAI under the account Codex is signed in with: the ChatGPT account, or an API key if Codex was set up that way. The run uses `--sandbox read-only`: Codex's commands cannot write or reach the network. That sandbox does not stop it from reading files your user can read. The prompt tells it to read nothing, and nothing in the prompt says where the job is. The run also uses `--ephemeral` (no local session record) and `--ignore-user-config` (no personal MCP servers or profiles; `--user-config` turns this off, and then your config also decides the provider). What OpenAI keeps follows that account's data controls.

**`living-picture codex plate [--job DIR] [--layer ID]`** prints guidance and sends nothing. It covers a large hidden area that the local fill cannot reconstruct (`cutting.md` section 4), and the local options to try first. It then explains what Codex's bundled imagegen skill can do. Its default built-in tool edits an image shown in the conversation, under the Codex sign-in. It takes no mask: the change is described in words, so the hole is marked in a flat colour. Masks exist only in its fallback CLI, which needs `OPENAI_API_KEY` and an explicit request. The image shown and the prompt leave the machine, and results land in `$CODEX_HOME/generated_images/`. The guidance also covers bringing the result back as a `plate` at the working size, and checking it against the painting's own marks at native and display size. SKILL.md rule 8 never concedes marks that do not come from the painting. So the default use of a generated plate is as a guide for rebuilding the hidden surface from the painting's own marks. Shipping generated paint itself needs the owner's explicit decision, recorded in `notes.md`.

## MCP server: not added

An MCP server is not worth it here:

- Every client this skill targets (Claude Code, Codex CLI, app and IDE) runs shell commands and reads skills natively. The CLI's JSON output already is the tool interface, and it works the same in a terminal.
- A server would restate every `picture.py` option in a second schema that must track each change. It would need registering in each client's config. Its tool schemas would sit in context every session, while a skill costs only its description until it is used.
- The heavy outputs here are image files that the agent opens by path. MCP adds nothing there.

Revisit this only for a client that has no shell, such as a chat-only app. Then a thin stdio server over `bin/living-picture` would be the way.

## macOS extras

- **A Finder Quick Action** in Shortcuts can start jobs from selected pictures. Use "Run Shell Script" with input passed as arguments: `for f in "$@"; do "$HOME/.local/bin/living-picture" new "$f" --job "$HOME/LivingPictures/$(basename "${f%.*}")"; done`. Then open the job in your agent.
  - Shortcuts gives the script a bare PATH. The CLI's launcher finds a real Python itself and never runs Apple's stub without the developer tools. The CLI adds Homebrew to PATH for `node` and `ffmpeg`.
  - Two pictures whose names differ only in case share one folder on a Mac. The second is refused, not mixed in; give it another `--job`.
  - This is a recipe and is not installed. The shell line was tested verbatim under zsh and bash with a bare PATH. The Shortcut itself was not built, and the launcher on a Mac without the Command Line Tools was simulated, not tried.
- **Where jobs go.** Keep job folders out of iCloud-synced Desktop and Documents if the pictures must stay on this Mac.
