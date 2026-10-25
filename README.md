# Algorithm Competition Assistant

![Tests](https://img.shields.io/github/workflow/status/chenmoulaile/algorithm-competition-assistant/tests?branch=main)
[Releases and downloads](https://github.com/chenmoulaile/algorithm-competition-assistant/releases)

> 中文完整手册：[README_CN.md](README_CN.md)

A competitive-programming test runner for **Sublime Text 4** (build 4095+): run the samples of the file you are editing
and get coloured verdict cards (AC / WA / TLE / MLE / RE / PE / CE), a detail tab with a line-by-line diff, **real** memory
and timeout verdicts, stress testing against a generator and a reference solution, [Competitive Companion](https://github.com/jmerle/competitive-companion)
browser import, and file input/output for the NOI / CSP-S style `freopen` problems.

**Built on**: a deep fork of [FastOlympicCoding](https://github.com/Jatana/FastOlympicCoding) (Jatana); the verdict display was rebuilt
after VSCode's [cph-ng](https://github.com/langningchen/cph-ng), and the Competitive Companion support was inspired by [FastOlympicCodingHook](https://github.com/DrSchwad/FastOlympicCodingHook).

## What it adds over FastOlympicCoding

| Capability | FastOlympicCoding | This package |
| --- | --- | --- |
| Coloured verdict badges (AC / WA / TLE / MLE / RE / PE / CE) | ❌ plain text only | ✅ cph-ng-style badges |
| **Real memory verdict (MLE)** | ❌ memory was a dead setting | ✅ samples the child process peak memory per platform, over the limit really is MLE |
| **Hard kill on timeout (TLE)** | ❌ an infinite loop survives | ✅ a watchdog terminates the whole process tree at the limit |
| **Run-time error location (RE)** | ❌ | ✅ parses `file:line` out of the output (Python traceback / Java stack / sanitizer diagnostics), replays the crash under gdb when it cannot |
| Detail tab + **line-by-line diff** | ❌ | ✅ a real editor tab, selectable and copyable; also shows the share of the time/memory limit used, the run before this one, and the subtask the test belongs to |
| **Compile cache** | ❌ recompiles every time | ✅ skips compilation when the sources and the command are unchanged |
| **Automatic C++ standard tier (saves 1.4-2 s per run)** | ❌ compiles whatever you hard-coded | ✅ builds the low tier first, escalates only when the compiler asks, remembers per problem file |
| Re-run failures only / run everything / force recompile | ❌ | ✅ three commands, three keys |
| **Float tolerance** (`float_tolerance`) | ❌ | ✅ numeric output compared by relative/absolute error |
| Multi-file builds (`extra_sources` / `include_dirs`) | ❌ | ✅ |
| Environment self-check (doctor) | ❌ | ✅ one command plus a paste-ready Markdown report |
| Keyboard-only workflow (pick a test, then act) | ❌ | ✅ `Ctrl+Alt+M` |
| Stress-test counterexamples saved automatically | ❌ | ✅ |
| **File input/output (NOI / CSP-S `freopen` problems)** | ❌ pipes only | ✅ per-problem file names, samples written to a temporary directory, the answer read back from the file your program writes |
| Persistent Competitive Companion listener | ⚠️ needs the Hook | ✅ listens from startup, creates a file per problem |
| Chinese / English UI | ❌ | ✅ |
| Run panel auto-fit width | ❌ | ✅ follows its cards in both directions - widens when they no longer fit, narrows when they do |

Engineering: Python 3.3 plugin-host compatibility, four-platform CI (Linux 3.9/3.12 + Windows + macOS), and a regression suite guarding the shipped behaviour.

## Install

**Folder install (recommended).** Clone or download `chenmoulaile/algorithm-competition-assistant`, put the repository
folder into `Packages`, restart. Any folder name works since v2.4.11: paths come from the directory (or zip name) the
package really lives in, `.sublime-package` suffix stripped.

**Package Control.** `Ctrl+Shift+P` → `Package Control: Install Package` → search `Algorithm Competition Assistant`; no restart needed, later upgrades use `Package Control: Upgrade Package`.

**Configuring a Package Control install.** Package Control installs a zip, so there is no package folder to browse.
Use `Preferences → Package Settings → Algorithm Competition Assistant → Settings`: the left pane is the read-only
defaults and the right pane is your editable `Packages/User/Algorithm Competition Assistant.sublime-settings`; saving
there survives upgrades. The same submenu opens Key Bindings, TestSyntax settings, and both READMEs.

⚠️ **Do not use both.** Package Control installs `Installed Packages/…sublime-package` (a zip); if a folder of the same name
also exists in `Packages/`, installing or upgrading the zip **empties that folder** — silently before v2.5.0, which is why "I
edited the code, restarted, nothing changed" happened. Startup now prints a notice and the environment check (`Ctrl+Alt+D`)
says whether the loaded copy is the `folder` or the `zip`.

## 30 seconds

1. Open a source file. Default `run_settings` cover C++ (`g++`, `-std=c++17 -O2`), Python, Java, C, Rust, Go, Pascal and JavaScript (Node.js); the compiler must be on `PATH`.
2. Press `Ctrl+Alt+B` and read the verdict cards in the run panel on the right.
3. Press **Detail** for input / expected / actual / stderr and the diff.
4. `Ctrl+Shift+P`, type `Algorithm` — every command is in the palette and `View → Algorithm Competition Assistant` mirrors it one for one. `-O2` matters: unoptimised, a correct program only looks like TLE.

## Key bindings

The bindings ship in this package's own `Default (<platform>).sublime-keymap`, so they load with the package and are given back
when you disable or uninstall it — nothing is written into your personal keymap. `Ctrl` and `Alt` are literal everywhere:
`Ctrl+Alt+B` is Control+Option+B on macOS too (no `super+` ships). Set `"enable_keybindings": false` to release the keys; `View → … → Key Bindings` opens the package keymap beside your override, and your side wins on a clash.

| Key | What it does |
| --- | --- |
| `Ctrl+Alt+B` | Run the tests |
| `Ctrl+Alt+Shift+B` | Run all tests, keep going after failures |
| `Ctrl+Alt+R` | Re-run failed tests only |
| `Ctrl+Alt+Shift+R` | Run with a forced recompile (ignores the compile cache) |
| `Ctrl+Alt+Shift+P` | Run all tests in parallel (`parallel_workers`, default 4) |
| `Ctrl+Alt+I` | Import tests from a file |
| `Ctrl+Alt+V` | Add a test from the clipboard |
| `Ctrl+Alt+E` | Export tests as `.in` / `.out` pairs |
| `Ctrl+Alt+C` / `Ctrl+Alt+Shift+C` | Copy the expected / actual output of the current test |
| `Ctrl+Alt+M` | Pick a test, then run / detail / edit / accept / decline |
| `Ctrl+Alt+S` | Start a stress test |
| `Ctrl+Alt+Shift+S` | Stop the stress test (stress output views only) |
| `Ctrl+Alt+L` | Competitive Companion: one new file per problem |
| `Ctrl+Alt+F` | Competitive Companion: fill the current file and run it |
| `Ctrl+Alt+Shift+L` | Stop the Competitive Companion listener |
| `Ctrl+Alt+Shift+F` | Fetch a problem from its URL (samples become tests) |
| `Ctrl+Alt+Shift+V` | Open the problem statement as Markdown |
| `Ctrl+Alt+Shift+K` | Benchmark the current test (N runs, min / avg / max) |
| `Ctrl+Alt+Shift+T` | Contest timer (status-bar countdown) |
| `Ctrl+Alt+G` | Switch Debug / Release build mode |
| `Ctrl+Alt+D` | Check the environment (compiler, port, paths, resources) |
| `Ctrl+Alt+J` | Jump to the first error location (palette also has "last") |
| `Ctrl+Alt+T` | Expand a template snippet |
| `Ctrl+Alt+P` or `Ctrl+K, Ctrl+P` | Collapse / restore the run panel |

Inside the plugin's own test editor and detail views (`source.TestSyntax` only): `Enter` insert line, `Ctrl+Enter` new test, `Ctrl+V` paste, `Ctrl+X` kill the process, `Ctrl+D` delete, `Ctrl+Shift+Up/Down` swap tests.

## The detail tab

`详情` / *detail* opens a real editor tab, so everything in it can be selected and
copied. On top of the line-by-line diff it reports what the run actually cost:

- **share of the limit** - `time used 1200ms/2000ms = 60%`, and the same for memory,
  because an AC that used 96 % of the limit is a trap on a slower judge;
- **the run before this one** - `vs last 300ms -> 1200ms (4.00x)`, plus
  best/avg/worst once there are three or more, so "did my edit help" has an answer.
  Per-test history lives in `<name>__tests.hist` next to the test data (20 runs kept);
- **size of the sample and of the answer** in lines and bytes;
- **which subtask group** the test belongs to and what that group scored;
- **which channel produced the answer** - a file-IO problem names `add.in -> add.out`,
  because the usual reason a detail is empty is a `freopen` name that disagrees
  with the statement.

Colour is applied with **regions and a phantom badge bar, not a syntax definition**: the
buffer is never modified, so selecting, copying and pasting give exactly what they gave
without it. The bar carries `copy expected output`, `copy actual output` and a close
button. Each section also gets a **card head that is the run panel's card row** - section
name, `fold` / `unfold`, `copy`, and the `12 lines / 340 bytes` value in a chip of its own,
with no arrow (the fold chip spells the state out). Set `"detail_rich": false` for the
card-free, colour-free view.

## File input/output (NOI / CSP-S)

Semifinal problems say "read `add.in`, write `add.out`" and expect `freopen`, which the default pipe-based runner cannot
serve. Set it per problem from `View → Algorithm Competition Assistant` or the palette (**This problem reads/writes files**);
both names are prefilled from the source file, and the choice lives next to the test data in `<name>__tests.io`, so it
follows that one problem and survives a restart.

The sample is written as `add.in` inside the run directory so the `freopen` finds it, and the answer is read back from the
`add.out` your program writes (stdout is drained and treated as debug output). **Your problem directory is never touched** —
the real data there belongs to the judge. A missing `add.out` is reported as such instead of handed to you as a blank WA.

A stress test (`Ctrl+Alt+S`) **refuses to start in file mode**, deliberately: the feeding goes through the pipes, so std,
generator and your program all reading and writing files would see no data and "agree" every round. Convert the three
programs to stdin/stdout to stress locally.

Detailed change history: [RELEASE_NOTES_CN.md](RELEASE_NOTES_CN.md)
