# Algorithm Competition Assistant

A local test runner for competitive programming in Sublime Text 4 (build 4095 or newer).
Open a source file, press `Ctrl+Alt+B`, and it runs your configured compile and run commands
over the test cases, then reports a verdict, a runtime and a memory figure for each one.
The `detail` tab holds the input, the expected output, the actual output, stderr and a
line-by-line diff.

This is a fork of [FastOlympicCoding](https://github.com/Jatana/FastOlympicCoding). The run
panel, the test editor and the shortcut layout come from that project. The way results are
displayed follows VSCode's [cph-ng](https://github.com/langningchen/cph-ng), and the browser
import follows [FastOlympicCodingHook](https://github.com/DrSchwad/FastOlympicCodingHook).

Most of the work went into judging: memory and time limits are measured from the real
process, runtime errors are located in your source when the information exists, compiles are
cached, and file I/O problems (NOI / CSP-S) run locally. It does not solve problems and it
does not submit anything.

Chinese manual: [README_CN.md](README_CN.md). Version history:
[RELEASE_NOTES_CN.md](RELEASE_NOTES_CN.md) (Chinese).

## Install

### Folder install (use this if you want to edit the code)

1. Clone or download the repository.
2. Put the folder into Sublime's `Packages` directory. Any folder name works.
3. Restart Sublime.

Sublime does not hot reload plugins, so a `.py` change needs a restart.

### Package Control

`Ctrl+Shift+P` → `Package Control: Install Package` → search
`Algorithm Competition Assistant`. Upgrades use `Package Control: Upgrade Package`.

Package Control installs a zip under `Installed Packages/`; there is no package folder to
browse. Configure it through `Preferences → Package Settings → Algorithm Competition
Assistant → Settings`: the left pane is the read-only default, the right pane is your
`Packages/User/Algorithm Competition Assistant.sublime-settings`, which survives upgrades.

Do not use both at once. If a folder of the same name sits in `Packages/` while the zip is
installed, installing or upgrading the zip empties that folder, and the only symptom is
"nothing changed after I edited the code". Startup prints a notice, and `Ctrl+Alt+D` reports
whether the loaded copy is a folder or a zip.

## Quick start

1. Open a source file. The default settings cover C++, C, Python, Java, Rust, Go, Pascal and
   JavaScript. The compiler has to be on `PATH`.
2. Press `Ctrl+Alt+B`. Each test gets a card in the panel on the right.
3. Click `detail` for input, expected, actual, stderr and the diff. Click `edit` to open the
   input and answer tabs; saving the answer rejudges the test.
4. The command palette (`Algorithm`) and `View → Algorithm Competition Assistant` expose the
   same commands.

The shipped C++ command uses `-std=c++17 -O2`. Keep `-O2`: without it a correct program can
look like a TLE locally.

## Judging

| Verdict | Meaning |
| --- | --- |
| `AC` | passed |
| `WA` | output differs from the answer |
| `TLE` | past the time limit, process terminated |
| `MLE` | peak memory over the limit |
| `RE` | non-zero exit, crash or failed assertion |
| `PE` | same tokens, different whitespace |
| `CE` | compilation failed |
| `SK` | stopped by hand |
| `UKE` | no verdict, for example the program never started |

After a run each card offers `accept` and `decline`: the first stores the current output as a
correct answer, the second marks it wrong. A test can keep several accepted answers, and a
run that matches any of them passes.

The clock starts when the process is created and stops the first time it is seen to have
exited. Plugin work sits outside that window: `Popen`, draining the pipes, reading stderr and
stopping the memory sampler. Feeding stdin does count, because the program is
reading it on a judge too. Serial and parallel runs use the same rule, so one binary cannot
be TLE in one mode and AC in the other. Limits are still compared strictly; the only slack is
up to 50 ms that decides when the watchdog acts, not how long the program may run.

Peak memory is taken per process: `GetProcessMemoryInfo` on Windows, `VmHWM` from
`/proc/<pid>/status` on Linux, `libproc.proc_pid_rusage` on macOS. Over the limit is `MLE`.

The watchdog terminates the process tree, so a loop that forks children does not survive
it.

A passing run that used 90-99% of the limit gets a status bar note. It does not change the
verdict.

When the program crashes, the output is searched for a position first: a Python traceback, a
Java stack or a sanitizer diagnostic gives `file:line`, shown on the card and in the status
bar, and the link jumps to the source. A bare C++ segfault carries no line, so the plugin
replays the crash under gdb and looks for your frame in the backtrace. That needs debug
symbols: a Release build only yields a name like `main ()`, while `Ctrl+Alt+G` (Debug)
locates the line.

Debug adds `-g -fsanitize=undefined -fsanitize-trap=undefined -D_GLIBCXX_ASSERTIONS`. The trap
form needs no sanitizer runtime, which matters because MSYS2's mingw ships neither `libubsan`
nor `libasan`. Signed overflow, stack array overrun and division by zero become a non-zero
exit (`RE`); `std::vector` overruns come from the assertion macro. Heap overruns are not
covered by default; add `-fsanitize=address` yourself if your toolchain has `libasan`.

Compilation is cached: unchanged sources (including local `#include "..."`) and an unchanged
command skip the compiler. `Ctrl+Alt+Shift+R` forces a rebuild.

The default starts at `std_floor` (`c++17`) and only moves up when the compiler asks for a
newer standard, remembering the tier in `<name>__tests.std` next to the test data. The reason
is measurable: parsing `<bits/stdc++.h>` costs 1.29 s at c++17, 2.43 s at c++20 and 2.53 s at
c++23 on the author's machine with `-fsyntax-only`, so a hard-coded c++23 adds a second to
every recompile. Ordinary syntax errors are reported as they are, without a retry.

Multi-file builds use `extra_sources` (glob) and `include_dirs`, referenced in the command as
`{extra_sources}` and `{include_dirs}`.

## The detail tab

`detail` opens a real editor tab, so the text can be selected and copied. It contains:

- input, expected output, actual output, diff, notes and stderr, each in a collapsible card;
- line and byte counts for the input and the answer;
- the subtask a test belongs to and that group's score, when `subtasks` is configured;
- the channel that produced the answer for file-I/O problems, for example `add.in -> add.out`.

Per-test history is still written to `<name>__tests.hist` (last 20 runs); the page does not
display those derived numbers.

Click a section name to fold or unfold it. A rejudge keeps the fold state.

Colour is applied with regions and card headers, not by changing the buffer. Select, copy and
paste give exactly the text on screen. Set `"detail_rich": false` for a plain, colourless
page; `"detail_style": "phantom"` is the old embedded panel, where the text cannot be
selected.

The detail tab shares the right column with the run panel, and that column is sized by the run
panel cards. Drag it wider if you need to; a width you set is not taken back automatically.

## File input/output (NOI / CSP-S)

Problems that read `add.in` and write `add.out` through `freopen` cannot use the pipe-based
runner. Set the mode per problem from `View → Algorithm Competition Assistant → This problem
reads/writes files`. Both names are prefilled from the source file (`add.cpp` gives `add.in` /
`add.out`), so two Enter presses usually finish it. The choice is stored in
`<name>__tests.io` beside the test data and follows that one problem across restarts.

At run time the sample is written as `add.in` inside a temporary directory, and the program
starts in that directory, so `freopen` finds it. The answer is read back from the `add.out`
your program writes. Your problem directory is not written to. When the program writes no
file, the report says so instead of turning an empty read into a WA.

Limits: stress testing refuses to start in file mode, because it feeds data through pipes and
none of the three programs would see it; every round would "agree". Convert the programs to
stdin/stdout for that. The gdb replay also feeds pipes, so a line number still works but file
reads fail.

If `run_cmd` calls the binary through a relative path (`./a.out`), file mode cannot find it,
because the program starts in the temporary directory. The shipped settings use the absolute
`{source_file_dir}` form.

## Stress testing

Three files in the same directory: your program (the open file), a reference solution
(`std.cpp` by default) and a generator that prints random data to stdout (`gen.cpp` by
default).

Press `Ctrl+Alt+S` in the source view. Where the pair comes from is controlled by
`stress_file_source`:

- `pick_once` (default): ask the first time for this file, then remember the choice in
  `<name>__tests.stress`;
- `same_dir`: look in the source directory only, trying `std` / `brute` / `bf` / `sol` /
  `暴力` for the solution and `gen` / `data` / `maker` / `rand` / `生成` for the generator,
  with no dialog; if nothing matches it says which directory it searched;
- `auto`: the old behaviour, directory first, then the global paths from last time, then ask.

Picking the same file twice is refused: a program compared with itself agrees every round.

Results scroll in a `<file> -stress` view. On a mismatch it stops and prints the input, both
outputs and the differing lines; after the last round it reports a pass. `Ctrl+Alt+Shift+S`
stops the session in the current view and takes effect immediately. Several files can run at
once, each with its own view.

The generator has its own limit (`stress_generator_time_limit_seconds`, 10 s by default),
because generating data is usually slower than solving. A timeout skips the round; three in a
row stop the run.

With a `checker` configured, the checker decides, so multi-answer and floating point problems
are not reported as mismatches. Without one, `float_tolerance` applies.

## Competitive Companion

With the browser extension installed, Sublime listens on port `12345` from startup
(`companion_auto_listen`, on by default). No file needs to be open first. Add `12345` to the
extension's port list; that part happens in the browser.

Two targets:

- `Ctrl+Alt+L` creates `<problem>.cpp`, stores the samples and limits, and stops there. It
  does not compile or run anything, since the code does not exist yet.
- `Ctrl+Alt+F` merges the samples into the current file and runs it once.

`companion_target` picks the default; the context menu and the palette offer both. In
`new_file` mode a pinned target is ignored.

A problem list page is not one problem. AtCoder and Luogu send the whole set in one click
(7 requests measured), so use `Ctrl+Alt+L` there. `Ctrl+Alt+F` would merge all seven problems
into the file you are standing in. Repeated auto-runs of the same file are debounced into one
(2 s), so that does not become seven compiles.

New files land in the first usable place: `auto_create_dir`, then the directory of the cpp
from which the command was invoked, then the current file's directory, then the first
workspace folder. `auto_create_header` customises the comment header with `{problem}`,
`{url}`, `{time}` and `{memory}`.

When a click does nothing, enable `companion_debug_log` and click again; the log is
`Data/Log/aca-companion.log`. A `refused 403 origin=…` line means the request arrived and was
rejected; an unchanged log means the browser never sent it, usually because `12345` is not in
the extension's port list.

## Interactive problems

Besides the automatic `interactor` mode, `Ctrl+Alt+Shift+X` opens a scratch view that runs the
current program with live output. `Enter` sends a line, `Ctrl+D` sends EOF, `Ctrl+X` stops the
program. This is for the cases where you are the interactor.

`interactor` follows the testlib convention: `interactor <input> <output> <answer>`, reading
the contestant's output from stdin and writing replies to stdout, with the exit code as the
verdict.

## Other tools

- Benchmark (`Ctrl+Alt+Shift+K`) runs the current test N times and reports min, average and
  max.
- Calibrate machine estimates this machine's speed factor and shows the equivalent judge
  limit in the status bar. Judging still uses the limit you set.
- Practice statistics (`Show practice statistics`) report attempts, first AC, verdict
  distribution and stress rounds per problem.
- Contest timer (`Ctrl+Alt+Shift+T`) counts down in the status bar, with notes at 30, 10 and
  5 minutes.
- Template snippets (`Ctrl+Alt+T`) expand after a keyword: fastio, main, bf, debug, testlib.
- Environment check (`Ctrl+Alt+D`) covers the compiler, port, paths and resources, and prints
  a Markdown report you can paste into an issue.

## Key bindings

The bindings ship in the package's own keymap, so they load and unload with it and nothing is
written into your personal keymap. `Ctrl` and `Alt` are literal: on macOS that is Control and
Option, and no `super+` binding ships. Set `"enable_keybindings": false` to release them all.

| Key | Action |
| --- | --- |
| `Ctrl+Alt+B` | run the tests |
| `Ctrl+Alt+Shift+B` | run all, keep going after failures |
| `Ctrl+Alt+R` | re-run failed tests only |
| `Ctrl+Alt+Shift+R` | recompile and run |
| `Ctrl+Alt+Shift+P` | run all in parallel (`parallel_workers`, default 4) |
| `Ctrl+Alt+Shift+X` | interactive terminal |
| `Ctrl+Alt+I` | import tests from a file |
| `Ctrl+Alt+V` | add a test from the clipboard |
| `Ctrl+Alt+E` | export `.in` / `.out` pairs |
| `Ctrl+Alt+C` / `Ctrl+Alt+Shift+C` | copy expected / actual output |
| `Ctrl+Alt+M` | pick a test, then an action |
| `Ctrl+Alt+S` | start a stress test |
| `Ctrl+Alt+Shift+S` | stop the current stress view |
| `Ctrl+Alt+L` | Companion: one new file per problem |
| `Ctrl+Alt+F` | Companion: fill the current file and run |
| `Ctrl+Alt+Shift+L` | stop the Companion listener |
| `Ctrl+Alt+Shift+F` | fetch a problem from its URL |
| `Ctrl+Alt+Shift+V` | open the statement as Markdown |
| `Ctrl+Alt+Shift+K` | benchmark the current test |
| `Ctrl+Alt+Shift+T` | contest timer |
| `Ctrl+Alt+G` | switch Debug / Release |
| `Ctrl+Alt+D` | environment check |
| `Ctrl+Alt+J` | jump to the first error |
| `Ctrl+Alt+T` | expand a template snippet |
| `Ctrl+Alt+P` / `Ctrl+K, Ctrl+P` | collapse / restore the run panel |

Inside the plugin's own test editor and detail views (`source.TestSyntax`): `Enter` inserts a
line, `Ctrl+Enter` adds a test, `Ctrl+V` pastes, `Ctrl+X` kills the process, `Ctrl+D` deletes
the test, `Ctrl+Shift+Up/Down` swaps tests.

## Settings

Four panels edit the keys in the settings file and take effect immediately:

| Entry | What it controls |
| --- | --- |
| `Verdict rules` | float tolerance, PE as AC, stop on first failure, ignore stderr |
| `Stress test options` | where std / gen come from, file picker, saving counterexamples, rounds, per-round limit |
| `Competitive Companion options` | default target, auto listen, click log |
| `This problem reads/writes files` | stdin or file I/O for this problem, and the file names |

`companion_auto_listen` is the one item that only takes effect the next time the listener
starts; the port is read at the same moment.

```json
{
	"language": "en",
	"run_settings": [
		{
			"name": "C++",
			"extensions": ["cpp"],
			"compile_cmd": "g++ \"{source_file}\" {extra_sources} {include_dirs} -std=c++17 -O2 -o \"{file_name}.exe\"",
			"run_cmd": "\"{source_file_dir}/{file_name}.exe\" {args}",
			"time_limit_ms": 2000,
			"memory_limit_mb": 256
		}
	],
	"float_tolerance": 0,
	"max_output_bytes": 8388608
}
```

The remaining keys are documented in `Algorithm Competition Assistant.sublime-settings`.

## Known limits

- macOS memory is a sampled peak for one process, not the kernel's peak RSS, so it can read
  lower than Activity Monitor.
- A bare segfault in a Release build only yields a function name. Use Debug, or add `-g`.
- `sync_output` is off by default. It refreshes the view per character and only suits
  interactive programs.
- stderr appears in the detail tab only, not on the card.
- A test with no input and no answer is not saved; it is a placeholder for a paste.
- The clock waits while the sample is still being pasted. It starts at the first typed
  character or the first line of output.
- A crash is judged `RE` ahead of `TLE`, even when the watchdog fired. Ordinary `cerr` output
  is not treated as a crash.
- `PE` requires identical tokens with different whitespace. Set `regard_pe_as_ac` if your
  judge counts PE as accepted.
- Output over `max_output_bytes` (8 MB by default) is truncated with a notice.
- Fetching has a size and time cap (`max_fetch_bytes` 8 MB, `fetch_timeout_seconds` 25 s).
  Pages rendered by scripts parse as empty, and the report says you may need to log in.
- On Windows, non-ASCII output names written through the ANSI code page may not match the name
  on disk. The plugin checks the artifact and runs the real name, with a console note.
- Stress testing is unavailable in file mode, and the gdb replay cannot read input files.
- The package does not hot reload: restart Sublime after editing a `.py`.

## Development

- The plugin host is Python 3.3. No f-strings, no `subprocess.run`, no
  `Popen(encoding=...)`; `tests/check_py33.py` enforces this.
- Tests: `python tests/run_tests.py`, currently 1044 checks, 0 failures, using a fake
  `sublime` module and no third-party packages.
- CI runs Linux (3.9 and 3.12), Windows and macOS.
- Bump `VERSION` in `plugin_init.py` and add `messages/<version>.txt` plus the
  `messages.json` entry; `tests/check_messages.py` checks the three agree.

## Credits

Built on FastOlympicCoding by Jatana. The verdict display follows cph-ng by langningchen, and
the Companion integration follows FastOlympicCodingHook by DrSchwad. MIT licensed, see
`LICENSE`.
