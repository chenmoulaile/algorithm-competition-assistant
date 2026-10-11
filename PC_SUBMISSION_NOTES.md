# Algorithm Competition Assistant — Package Control Submission Notes

Package version: v2.4.6
Repository: https://github.com/chenmoulaile/cph-by-chenkx
Channel entry file: `repository/a.json` (see snippet at the bottom)

## v2.4.6 notes

A correctness and responsiveness pass over paths that failed quietly. No change
to how a run is started or judged, apart from the two verdict fixes below.

- **Debug mode no longer fails its first compile.** The default flags were
  `-fsanitize=address,undefined`; this toolchain ships neither `libasan` nor
  `libubsan`, so every switch to Debug first produced
  `ld.exe: cannot find -lubsan` and then relied on the retry that strips
  `-fsanitize`. The default is now
  `-fsanitize=undefined -fsanitize-trap=undefined`, which needs no runtime
  library: a trap turns each UBSan check into `ud2`, still a non-zero exit code
  (RE), with the source line coming from the existing gdb replay. A user-written
  `-fsanitize=address` in `compile_cmd` is still honoured. The sanitizer-stripping
  regex was also widened from `-fsanitize=\S+` to `-fsanitize\S*`, because the
  old form left `-fsanitize-trap=undefined` behind as an orphan and silently
  turned undefined behaviour into `ud2` in Release builds.
- **A crash that still exits 0 is now judged RE.** Aborting in a destructor or a
  signal handler, and sanitizers that call `_exit(0)`, all leave exit code 0, so
  "prints the right answer, then crashes" was AC. The crash-text check now runs
  before the output comparison.
- **Subtask scoring** no longer counts a group as passed when one of its tests
  was never judged.
- **Two quadratic paths on the UI thread were fixed.** The line diff in the
  detail view trims the common head/tail first and falls back to a linear scan
  past a cell budget (2000 lines of `0`/`1` output with 20 edits took 56 s);
  the detail view lists at most 200 differing spots and states how many were
  omitted. `html_to_markdown()` replaces its per-block `str.replace()` loop with
  a single regex pass (8000 blocks: 2.44 s → 0.246 s).
- **The practice-statistics log is capped** at the newest 5000 entries; the
  per-problem counters stay permanent, so "solved" cannot regress when an old
  AC entry is dropped.
- **Silent failures now say so:** auto-creating a file for a listener when the
  directory is not writable, saving the statistics file, clearing the test
  files, saving the stress counterexample, and a partial batch import.
- **Importing a directory as test data** reported `[Errno 13] Permission denied`
  (what Windows raises for `open()` on a directory); all four import paths now
  name the problem, and a directory is no longer a candidate `.out` file.
- Added: the stress run summary (rounds / compared / generator timeouts), stress
  rounds in the statistics, a localized statistics report, `max_fetch_bytes`
  (8 MB, read in 64 KB chunks) and `fetch_timeout_seconds` as settings, a
  one-line statement summary after fetching a problem, and a once-per-file hint
  that a WA in Release mode may be invisible undefined behaviour.
- Removed nine functions and 32 i18n strings that no code path could reach,
  with a guard that fails when a string is added without a caller.

## v2.4.5 notes

Follow-ups to the 2.4.4 rename:

- `messages/install.txt` (the first-install screen) still said `cph-by-chenkx`;
  it now uses the published name.
- The two fold-arrow region icons were the last hardcoded
  `Packages/Algorithm Competition Assistant/...` paths; they now follow
  `base_name` like every other resource, so a renamed folder no longer breaks
  them.
- The stress "replay last counterexample" worker wrote to its view from a
  background thread; its output is now posted to the main thread, matching the
  main stress loop.
- `.workbuddy-ai/` added to `.gitignore` and to `.gitattributes`
  `export-ignore`, so assistant scratch never ships inside the package.

## v2.4.4 notes

- The published package name is **Algorithm Competition Assistant** (the
  channel entry carries an explicit `name`; the repository keeps its URL).
  The old name `cph-by-chenkx` was opaque - "cph" is also Copenhagen Airport -
  and the maintainer suggested a descriptive name.
- The rename is applied consistently inside the package, which is what the
  reviewer bot failed over:
  - default settings file is now
    `Algorithm Competition Assistant.sublime-settings` (a settings file has to
    be named after the package or a syntax you ship)
  - palette entries are `Preferences: Algorithm Competition Assistant ...`
  - `Main.sublime-menu` has a `Package Settings` entry with that name
  - every `${packages}/...` path points at the installed package name
- The channel entry moved from `repository/c.json` to `repository/a.json`.
- User data files under `Packages/User/` keep their previous names on purpose,
  so an existing calibration/statistics file is not orphaned.

## v2.3.0 notes

- Fixed a compile command that was tidied *inside* quotes as well: a file
  named `P2517  ZJOI 2010, 基站选址.cpp` (two spaces) was passed to cc1plus
  with one space and reported as "No such file or directory". Whitespace is
  now tidied outside quotes only; covered by tests that keep a quoted
  double space intact in both build modes.
- The compile error is no longer rendered twice: the panel shows the
  "Compilation Error" chip plus the clickable `file:line` diagnostics, and
  the full compiler output stays as the plain, copyable text below it.
- Every file under test gets its own `<name> -stress` page. Previously a
  second file jumped into the page of the first one. Several files can be
  stress tested at once now: one session per page, each with its own scratch
  build directory and its own Stop; closing the page stops that run.
- `Ctrl+Alt+Shift+S` (macOS `Cmd+Alt+Shift+S`) is bound to the new
  `cph_stress_view` context key, so it stops the run of the page you are
  looking at and does nothing elsewhere.
- Stress builds drop `-DLOCAL` / `-DDEBUG` from the compile command (the
  user's own Run keeps it), and the shipped default C++/C commands no longer
  carry `-DLOCAL`.
- tests/run_tests.py: 275 -> 294 checks.

## v2.1.1 notes

- Fixed the two findings the channel's automated review reported on v2.1.0:
  `messages.json` used the key `"2.0"`, which is not a valid semantic version
  (now `"2.0.0"`), and three `subprocess.Popen` calls did not hide the console
  window on Windows (`startupinfo` with `STARTF_USESHOWWINDOW`).
- Two regression guards were added so neither can come back: every
  `messages.json` key must be `install` or a three-part version, and every
  `subprocess.Popen` call in the package must pass `startupinfo`.

## v2.1.0 notes

- **Stress testing now follows the judging rules**: the two programs' outputs
  were compared line by line, which made stress testing unusable for
  multi-solution problems (any valid answer "differs") and for floating point
  problems. When a `checker` is configured it decides instead (the same
  testlib/stdin checker the judge uses); otherwise `float_tolerance` applies.
  Nothing else about the stress flow changed.
- **New command** `cph_import_tests_here`: loads every `*.in` / `*.out` pair in
  the directory of the file being solved (no path prompt). It reuses the
  existing folder-import code, so the merge/dedup policy is unchanged.
- **New default language**: JavaScript (Node.js), `node <file>`, no compile.
- Still no root-level plugin imports, no `sys.path` changes, no bindings
  without a context, tip of main tagged, CI green on Linux 3.8/3.12 +
  Windows + macOS.

## v2.0.1 notes (bug fix)

Four defects found by driving every feature through its real command path in a
Sublime simulator (a dev-only harness that dispatches the plugin's real
commands against real buffers):

- the program's stdin was never closed, so anything that reads to EOF
  (`sys.stdin.read()`, `while (cin >> x)`) hung and was reported TLE;
  `ProcessManager.close_stdin()` is called after a stored sample now (and only
  then - the manual paste flow and the interactor keep stdin open);
- a command-style checker/interactor (`"python checker.py"`) was pushed through
  the compile command and never ran;
- `cwd=os.path.dirname(exe)` raised TypeError for a command-style interactor,
  so every interaction was UKE;
- the interactor's stderr was never drained (invisible testlib quitf messages,
  and a full pipe would block the interactor).
- Regression checks: 233 -> 243; the feature harness has 46 assertions.

## v2.0 notes

- New root-level plugin files (Sublime loads the commands from them):
  `cph_jump.py`, `cph_extras.py` (fetch / statement / benchmark / contest timer
  / stats / calibrate) and `cph_auto.py` (post-save listener). All shared logic
  lives in `core/`, because root-level plugin modules may not import each other.
- New `core/` modules: `cph_checker.py` (SPJ), `cph_interactive.py`
  (interactor relay), `cph_parallel.py` (worker pool), `cph_subtasks.py`,
  `cph_stats.py`, `cph_calibrate.py`, `cph_fetch.py`, `cph_html.py`,
  `cph_jump.py`, `cph_build_mode.py`.
- Everything user-visible is opt-in: `auto_run_on_save`, `auto_format_on_save`
  and `contest_duration_minutes` default to off, and the new features only
  activate when their `run_settings` key (`checker` / `interactor`) is set.
- The package still never writes the user's settings file. The two data files it
  owns live in `Packages/User/` (`cph-by-chenkx-stats.json`,
  `cph-by-chenkx-machine.json`).
- Six new shipped bindings, all behind `cph_keybindings_enabled`, plus eight new
  command palette entries.
- Regression checks: 168 -> 233.

## v1.4.19 notes

- **New files** (all in the root folder, so Sublime loads the commands):
  `cph_jump.py` (the `cph_jump_to_location` command) plus `core/cph_jump.py`,
  `core/cph_build_mode.py` and `core/cph_parallel.py`. The root module imports
  the helpers from `core/`, which is where shared code has to live (root-level
  plugin modules may not import each other).
- **Clickable error locations.** `core/cph_jump.py` parses g++/clang and MSVC
  diagnostics (capped at 20 entries) and renders them as minihtml links; the
  targets are stored in the view setting `cph_jump_targets` because a minihtml
  callback only receives the href string. The detail view gets a phantom at
  the top (a plain text view cannot have links), the compile bar gets a
  clickable list below the raw compiler output, and both now carry a phantom
  callback.
- **Debug/Release build mode** (`core/cph_build_mode.py`). The transform is
  applied inside `ProcessManager.get_compile_cmd()`, so the compile, the cache
  fingerprint and doctor all agree. The mode is per source file and lives in
  memory only - the package still never writes the user's settings. New
  setting `build_mode`; the run panel's status label gained a `DEBUG` chip.
- **Parallel test execution** (`core/cph_parallel.py`, command
  `cph_run_parallel`, setting `parallel_workers`). Each test gets its own
  ProcessManager; the worker threads never touch a view (results come back
  through `sublime.set_timeout`). The new `parallel` flag is threaded through
  `CphViewTesterCommand.run` -> `create_opd` -> `make_opd` exactly like
  `run_all`/`run_failed`.
- Two new shipped bindings (`ctrl+alt+g`, `ctrl+alt+shift+p`), both behind the
  `cph_keybindings_enabled` context key, plus two new command palette entries.
- Regression checks: 168 -> 188.

## v1.4.18 notes (hotfix)

- **v1.4.17 could not compile or run anything.** `make_opd()` iterated the
  tests with `for t in tests:`, and `t` is the i18n helper imported at module
  level. Python makes a name local for the whole function as soon as it is
  assigned anywhere in it, so the later
  `self.set_compile_bar(t('compiling'))` raised
  `UnboundLocalError: local variable 't' referenced before assignment`.
  The loop variable is renamed; the same latent pattern in
  `_source_fingerprint()` (a loop variable named `path`, shadowing
  `from os import path`) is fixed too.
- **New static guard for the whole bug class**: `tests/run_tests.py` walks
  every plugin file with `ast` and fails when a function both assigns to a name
  and calls it, while that name is also a module-level import/definition.
  Verified by injection (renaming the loop variable back to `t` is caught).
  `make_opd.__code__.co_varnames` is also pinned to contain no local `t`.
- Removed unused imports / dead locals (`sys`, `subprocess`, `shlex`,
  `VERDICTS`, `set_lang`, ... in `test_manager.py`; `cph_stress.py`,
  `cph_language.py`, `cph_tests_io.py`, `test_edit.py`). pyflakes reports no
  shadowing/undefined problem in the files touched by this release.
- Regression checks: 166 -> 168.

## v1.4.17 notes

- **Key bindings moved back into the package keymap.** They had been copied
  into the user's own `Packages/User/Default (<platform>).sublime-keymap`
  (to satisfy the "no keybindings by default" advice), which meant they
  survived disabling the package and blocked other packages from using those
  keys. Every binding still carries a concrete context; the 13 code-file ones
  additionally carry the new `cph_keybindings_enabled` context key, which is
  driven by the new `enable_keybindings` setting (default true) - so the keys
  can be released without editing any keymap. `tests/run_tests.py` keeps both
  rules (every shipped binding has a context; code-file bindings are gated)
  and no longer requires the bindings to live outside the package.
- **RE vs false TLE.** `get_verdict_by_code()` now also looks for crash
  signatures in `stdout` when the runtime is over the limit; it only checked
  `stderr`, so with `separate_stderr` off the crash text was invisible and an
  aborted program was reported as a plain TLE. TLE verdicts now also carry a
  diagnostic message when the program produced no output at all, or when the
  compile command contains `-DLOCAL` / `-DDEBUG`.
- **Stale verdicts.** `Test.reset_run_state()` plus a snapshot of the
  previously-accepted indices in `make_opd()`: a fresh run clears the verdicts
  loaded from the tests file, so the summary no longer counts the previous
  run's results (and `Run failed tests` still skips what was accepted).
- **Closing the source file closes its panel**: `CloseListener.on_pre_close`
  now also closes the paired `<file> -run` view and the `test N -edit` /
  `-answer` tabs opened from it.
- The shipped C++ `compile_cmd` uses `-std=c++17 -O2` (measured: `-O2` costs
  almost nothing to compile, `-std=c++23` costs ~1.3s more per compile than
  `c++17`).
- `sync_read_only()` only calls `view.set_read_only()` when the value changes.
- Regression checks: 158 -> 166.

## v1.4.16 notes

- `Highlight/test_edit.html` is restored to the v1.4.13 content (byte-identical
  to the file at tag v1.4.13, blob sha
  `dc7b0ad1cd034270b97719d56109e54cdb256786`). v1.4.14 had packed the template
  onto one line; the whitespace inside each `<a>` is what minihtml collapses
  into the chip's inner padding, so removing it made the card look cramped.
  No CSS changed between v1.4.13 and v1.4.14 (`test_styles.css` is
  byte-identical in both).
- `tests/run_tests.py` pins that template shape now: the card must not start
  with a blank line, the chip inner padding must stay, and no label may sit
  flush against its tag. Regression checks: 155 -> 158.

## v1.4.15 notes

- No new files or settings surface changes; runtime/UI fixes plus one data-model
  invariant:
  - the edit view keeps a sentinel newline on line 0 again. A block phantom is
    always drawn *below* the line it is anchored to (minihtml has no
    LAYOUT_ABOVE, sublimehq/sublime_text#4469), so without the empty first line
    the button card landed between the first and second line of the sample.
    `_content()` saves from position 1 and `EditModifyListener` keeps the caret
    at >= 1, so the sentinel never reaches the stored sample. This is the same
    layout the `-run` panel has used all along (`erase_all` leaves a `'\n'`).
  - `load_all_tests()` now filters with `is_meaningful_test()`, the same
    predicate `save_tests()` uses: a stale `{"test": ""}` in *any* candidate
    file used to be merged back on every reload.
  - `save_tests()` writes every candidate path that already exists, so a test
    deleted in the panel cannot come back from a stale copy (`load_all_tests()`
    merges all of them).
  - `Tester.prog_out` is kept as long as `Tester.tests`; `get_tie_pos()`,
    `toggle_fold()` and `check_test()` use the bounds-safe `output_at()`.
    IndexError there made "delete test" fail silently for a test the session
    had not run yet.
  - `test_edit` only pushes back the side whose buffer it actually read: closing
    `-answer` and saving from `-edit` used to clear the stored answer.
  - `cph_stress` uses the same placeholder set and the same lenient formatter as
    `ProcessManager.format_command()` (`{file}` raised `KeyError`).
  - `Test.__init__` tolerates a missing `test` key and non-string answers.
  - A central `ACTIONS_NEEDING_TESTER` guard covers the actions that index the
    test model, so a panel whose compile failed (no tester) no longer raises
    `AttributeError` from Ctrl+D / swap / the test menu.
  - `get_verdict_by_code()`: the 137/9 -> MLE and 124/142 -> TLE branches were
    unreachable behind `is_crash_exit_code()` (128..192); reordered.
- Regression checks: 138 -> 155.

## v1.4.14 notes

- No new files or settings surface changes for the review; the changes are runtime
  fixes plus template/UI cleanups:
  - the stderr badge was dropped from the test card (detail view keeps it),
  - a pristine placeholder test (no input, no answer) is no longer persisted by
    `save_tests()` (`is_meaningful_test()`), which fixed problems keeping a
    `[{"test": ""}]` entry forever,
  - `Ctrl+D` deletes tests that were never run,
  - crashes are judged RE (crash exit codes and crash signatures), including when
    the watchdog fired,
  - each run has a generation so a stale watchdog / listener thread from the
    previous test cannot kill or corrupt the next one,
  - the run clock and the TLE watchdog stay paused until the sample is pasted,
  - the accept/decline decision now ignores whitespace exactly like the judge,
  - reopening an edit tab replaces its content instead of appending it again.
- Regression checks: 116 -> 138.

## v1.4.13 notes

- The "Key Bindings" menu entry pointed at `${packages}/Default/Default ($platform).sublime-keymap`;
  the reviewer requires `base_file` to live inside the submitted package, so it now
  references this package's own `Default ($platform).sublime-keymap` (the split view
  still edits `Packages/User/Default ($platform).sublime-keymap`). `tests/run_tests.py`
  gained a check that menu entries never reference another package's directory.
- Regression checks: 115 -> 116.

## v1.4.12 notes (Package Control review guidelines)

- **No key bindings ship by default.** The 14 bindings that used to fire while
  editing source code moved to `Example (Windows|Linux|OSX).sublime-keymap` as
  suggestions (the review guide asks packages not to claim keys; users copy what
  they want into their own keymap). Bindings scoped to `source.TestSyntax` (our
  own test edit / detail views) and bindings behind a custom state context key
  (`cph_has_run_panel`, `cph_stress_running`, `cph_listener_running`) still ship,
  because they cannot affect editing in other packages' files.
- **The context menu is conditional** and reduced to six entries. A
  `.sublime-menu` item supports no `context` key, so visibility is decided by the
  commands' `is_visible()` - the mechanism Default's own `open_context_url` uses.
  `core/cph_target.py` answers "is this a file this package can run?" from
  `run_settings`, and the new `context_menu` setting turns the entries off.
- **`edit_settings` instead of `open_file`** for Settings and Key Bindings, so
  they open in a split view as the guide requires.
- **README now has an English section** (purpose, installation, getting started,
  key bindings, context menu).
- Requires Sublime Text 4 (`>=4095`): the minihtml card styling uses ST4 CSS
  variables (`color(var(--foreground) alpha(...))`).
- Regression checks: 108 -> 115. Ten of them encode these rules: shipped bindings
  must never claim keys used for editing, suggestion keymaps must be scoped,
  context menu commands must implement `is_visible()`, settings/key bindings must
  use `edit_settings`, `context_menu` must be shipped in the default settings.

## v1.4.11 notes

- `Modules/build_artifact.print_safe()`: a console message naming a mangled
  non-ASCII binary raised `UnicodeEncodeError` on the cp1252 Windows CI runner
  and aborted the test job. It now retries with an ASCII-escaped rendering.
  `cph_stress.py` uses it for every message that embeds a file name.
- `MemorySampler` also stops after `max_silence` (2s) without a single
  successful sample, not only after 20 consecutive misses: sampling a
  non-existent pid is slow on macOS and the thread outlived the join window
  there.
- Regression checks: 107 -> 108 (the new one reproduces the cp1252 failure by
  redirecting stdout to a cp1252 encoder).

## v1.4.10 notes

- New `Modules/build_artifact.py`. On Windows a compiler creates the `-o` file
  through the ANSI codepage, so `A 中文.exe` lands on disk under the GBK bytes
  read back as latin-1 and `os.path.exists()` is False - every run of a source
  file with a non-ASCII name ended in `FileNotFoundError`. `resolve_artifact()`
  finds the file the compiler really wrote (exact name, codepage round trip,
  then the newest candidate of this compile), and `retarget_command()` /
  `retarget_path()` point the run command at it. Only the executable token is
  touched, only when it is missing, and never for a PATH command (`python`).
- `ProcessManager.compile()` records what the compiler produced
  (`artifact_paths()` / `_artifact_note()`), so a successful compile that wrote
  nothing now says so instead of failing later with "file not found".
- `cph_stress.py`: the generator has its own time budget
  (`stress_generator_time_limit_seconds`, default 10s) and its timeout no longer
  aborts the run as a failure; a failed compile prints the compiler exit code
  and first error line; a generator that exits non-zero prints its exit code and
  stderr; a program that cannot start is reported instead of being compared as
  an empty (wrong) answer; `_compile_program()` verifies the artifact and
  returns a reason.
- `test_manager.py`: `get_view_by_id()` and `make_opd()` survive a view that is
  no longer attached to a window (was `AttributeError: 'NoneType' object has no
  attribute 'views'`, leaving an empty `-run` tab); `get_clipboard()` is
  guarded. `CphStartStressTestCommand` reuses the remembered source file when
  invoked from the scratch `-stress` view.
- Regression checks: 86 -> 107.

## v1.4.9 notes

- **Run modes fixed**: `cph_run_all_tests` / `cph_run_failed_tests` /
  `cph_run_force_recompile` sent `run_all` / `run_failed` / `force_compile` to
  `cph_view_tester`, whose `run()` did not accept them -> `TypeError`, so the
  three commands were dead outside the run panel. They are now accepted and
  forwarded to `create_opd()`.
- **Runtime errors are located**: when a crashed program's output names a
  position (Python traceback, Java stack trace, gcc/clang `-fsanitize`
  diagnostic, libstdc++ assertion) the card/detail and the status bar show
  `file:line`. A bare C++ segfault carries no line of its own, so the plugin
  replays it under gdb (`core/cph_gdb_trace.py`) and reads the frame out of
  the backtrace; that needs `-g`, which the debug build mode
  (`Ctrl+Alt+G`) adds - see the README for the measured difference.
- **`ProcessManager`**: the stdin-closed handler called `t()` without importing
  it (`NameError`); `cph_stress` leaked the `except ... as e` variable into a
  deferred lambda (same class of bug); a stress run whose every round timed out
  no longer reports success.
- **Test chain**: `run_failed` mode now continues past a failing test; running
  a not-yet-materialised test from the test menu no longer raises `IndexError`
  (new `Tester.output_at()`); deleting a test no longer rewinds `test_iter`.
- **`tests_relative_dir`** is now probed by `get_tests_paths()` (previously the
  data was written but never loaded or cleared); single-file import now uses the
  shared `merge_into_file()` policy; `_read_text()` detects a UTF-16 BOM first.
- **Cards**: the status class emitted by `get_test_class()` matched no CSS rule
  (`test-AC` / `test-wrong-answer` vs the shipped `.test-accept` /
  `.test-decline`), so the green/red tint was dead - fixed; missing verdict
  badge colours added; the card now surfaces a captured stderr.
- Regression checks: 66 -> 86.

## v1.4.8 notes

- **doctor was unusable**: `cph_doctor.py` ran its placeholder check without
  importing `re`, so opening "Check environment (doctor)" raised `NameError` on
  the first language entry and the report view was never created. The import is
  restored; `tests/run_tests.py` now both checks `_unknown_placeholders` and
  executes the full `run()` path, so a "feature totally dead" regression of this
  kind is caught locally.
- **Memory chip no longer wraps**: `get_nice_memory()` used a plain space, which
  minihtml broke onto two lines on a narrow panel; it now uses `&nbsp;` (the
  plain-text detail view converts it back).
- Trailing newline added to `repository-cph-by-chenkx.json` and
  `Main.sublime-menu`; regression checks 62 -> 66.

## v1.4.7 notes (Package Control review round)

Reviewer feedback addressed:

- **Root level plugin imports** - `cph_doctor.py` imported `.cph_companion`; the
  listener state now lives in `core/cph_state.py` and doctor reads it from there.
- **Key bindings without a context** - the panel collapse/restore, stress stop,
  Companion stop and doctor bindings now carry contexts. A new listener
  (`cph_context.py`) answers four custom context keys (`cph_run_view`,
  `cph_has_run_panel`, `cph_stress_running`, `cph_listener_running`), so keys
  only fire when they actually apply.
- **Platform settings variant** - `cph-by-chenkx (Windows).sublime-settings` was
  removed; the shipped default command is already platform neutral.
- **Channel entry** - the `name` key was dropped (derivable from `details`).
- `tests/run_tests.py` now enforces these four rules (62 checks in total).

## v1.4.6 notes

- Fixed a stale run-view check in `cph_tests_io._refresh_panel`: the panel marker
  became a view setting in v1.4.5, so the refresh silently did nothing when the
  command ran from the panel itself.
- One shared merge policy (`core/cph_tests_merge.py`) for tests from files, the
  clipboard, the Companion listener and stress counterexamples: keyed by input,
  answers are only filled in - a re-sent sample can no longer overwrite an answer
  the user accepted by hand.
- `insert()` stops retrying a closed stdin pipe; unknown command placeholders are
  surfaced in the compile panel and checked by doctor.
- New `regard_pe_as_ac` setting, a copy-pasteable doctor Markdown report, and
  `cph_test_menu` (Ctrl+Alt+M) for mouse-free test operations.
- New static regression checks in `tests/run_tests.py`: every `self.x()` call must
  be defined in its class, and the retired `opd_info` marker must not be compared
  anywhere. Both are the kind of defect that unit tests alone cannot reach.

## v1.4.5 notes

- Fixed the shipped default C++ `compile_cmd` raising `KeyError: 'extra_sources'`
  on every fresh install: `${extra_sources}` / `{include_dirs}` are expanded
  before formatting now, and unknown placeholders only warn (`_LenientFormat`).
- macOS memory now comes from `libproc.proc_pid_rusage(pid, RUSAGE_INFO_V2)` for
  the measured process instead of the cumulative `RUSAGE_CHILDREN` peak.
- `Test.rtcode` is initialized, doctor recognises template commands, cards refresh
  after a swap, shifted detail tabs are closed, a broken stdin pipe is survivable,
  edit tabs are reused, truncation is visible, the process group is cached for the
  SIGKILL escalation, Companion samples dedupe by input, clipboard pastes are
  inserted in one go.
- CI now runs on Windows and macOS as well as Linux; 47 regression checks.

## v1.4.4 notes

- Newlines are normalized (CRLF and lone CR to LF) on every text path: program
  stdout, stderr, compiler diagnostics and stored test data. The binary pipe
  introduced for Python 3.3 compatibility had dropped universal-newline
  translation, so Windows CRs were rendered as `<0x0d>` in the run panel.
  A CRLF split across two reads still yields exactly one newline.
- Peak memory is sampled synchronously right after `Popen` as well as by the
  polling thread, so very short runs (tens of milliseconds) still report it.
- `tests/run_tests.py` grew to 36 checks (newline handling included).

## v1.4.2 notes

- Fixed three regressions from the previous round: the "re-run failed tests"
  chain raised `IndexError` when it skipped accepted tests (the iterator was
  padded a single slot), the `sync_output` setting was unreachable because
  the command signature defaulted to `sync_out=True`, and the run summary
  phantom ended up inside a test after expanding a folded AC test.
- The edit/answer panel bar is now split over two lines (`test N` as a
  heading, buttons below it).
- `#include "..."` headers (depth 3) are part of the compile cache key, so
  editing a header triggers a rebuild instead of running a stale binary.
- POSIX only spawns through a shell when the command really needs one
  (pipes/globs/`&&`); otherwise `shlex.split` + `shell=False`, so the memory
  sampler measures the program and `killpg` reaches it.
- Compiler diagnostics decode as UTF-8 and fall back to the locale encoding
  (Chinese g++ messages were garbled).
- i18n: the HTML templates now use the `next_label` / `stop_label` /
  `test_label` / `time_label` / `type_label` placeholders the code always
  passed; the Chinese UI no longer shows those words in English.
- Dead code removed: `LayoutListener.move_syncer`, empty `isEnabled`,
  unused `REGION_*_PROP` tables, `BEGIN/OUT/END_TEST_STRING`, the obsolete
  `Test N {` / `} rtcode` highlight rules, the whole `use_debugger` plumbing,
  `ProcessManager.get_path/has_var_view_api/new_test/communicate`.
- `CphViewTesterCommand` state is per view instead of class attributes.
- **Added `tests/` and CI**: `tests/run_tests.py` (26 checks, fake `sublime`
  module, no third-party deps) and `tests/check_py33.py` (Python 3.3 guard
  that already proved it catches the `Popen(encoding=...)` regression), both
  run by `.github/workflows/tests.yml` on push/PR. `tests/`, `.github/` and
  the dev documents are `export-ignore`d, so they stay out of the
  `.sublime-package`.

## v1.4.1 notes (packaging-relevant)

- Sublime's plugin host is **Python 3.3**: `Popen(encoding=..., errors=...)`
  (3.6/3.7+) crashed every run. Subprocesses now use binary pipes with an
  incremental UTF-8 decoder, and `subprocess.run` was removed from the stress
  test (it does not exist before 3.5).
- Process state self-heals after a failed `Popen`
  (`is_run` / `is_stopped` / `terminate` / `insert`).
- No background writes to the user's settings file: the limits received from
  Competitive Companion are kept in memory.

## v1.4.0 notes (packaging-critical)

- **Resources are now loaded with `sublime.load_resource()`** through the new
  `core/cph_resources.py`. Previously every `Highlight/*.html` / `*.css` was read
  with a real filesystem `open()`, which raises `FileNotFoundError` once the
  package is installed as a `.sublime-package` (zip) — every card, detail view
  and compile panel would have failed to render. Reads are cached per process.
- `Modules/__init__.py` and `Highlight/__init__.py` added so all subpackages
  load reliably from a zip (no reliance on implicit namespace packages).
- `messages.json` + `messages/install.txt` added (PC install message with the
  key bindings).
- `.gitattributes` added: dev-only files are `export-ignore`d.
- Channel metadata uses `"sublime_text": ">=4095"`: the minihtml CSS uses ST4
  CSS variables (`var(--foreground)`, `color(... alpha(...))`).
- Root `__init__.py` remains comment-only; `plugin_init.py` is the entry point;
  no `package-metadata.json`, no `.no-sublime-package`.
- Memory limit verdicts are real: peak memory is sampled per platform
  (`Modules/memprobe.py`) instead of being dead configuration.
- Process termination kills the whole process tree on POSIX (`killpg`, SIGTERM
  → SIGKILL) and on Windows (`taskkill /F /T`).

## v1.2.0 notes

- Import commands moved from `core/` subpackage back to root-level
  `cph_import.py` — Sublime does not auto-load commands defined inside
  subpackages, which left the menu items greyed out.
- Imports append/merge with existing tests (dedup by input+answer) instead of
  overwriting the session.
- Re-run kills a still-running process before starting a new one (epoch-based
  stale callback drop); fixes the "next test" hang.
- New `detail_style` setting (`view` default / `phantom` legacy), sample input
  section in detail, lone `\r` cleanup.
- Edit view top bar reordered (save/delete before hint) so buttons stay
  clickable at default sidebar width; stale edit/answer views closed on re-run.
- Verdict badge CSS: plain inline element so the colored background hugs the text.

## v1.1.0 notes

- Shared modules live in the `core/` subpackage (`cph_settings`, `cph_i18n`,
  `cph_verdict`); import commands restored after being accidentally dropped.
- All root-level plugins import only from subpackages (`.core.*`, `.Modules.*`,
  `.Highlight.*`); root `__init__.py` stays comment-only.
- New commands: `cph_companion_listener` (idempotent, persistent),
  `cph_companion_stop_listener`, `cph_test_detail_view`.
- No `package-metadata.json`, no `.no-sublime-package` in the repository.

## v1.0.7 notes — 16 Failure Fixes (all verified)

1. Root `__init__.py` empty/comment-only — no root-level plugin imports.
2. `core/` subpackage created for settings / i18n / verdict modules.
3. `core/__init__.py` subpackage entry.
4. Root `__init__.py` kept comment-only; entry point deferred to `plugin_init.py`.
5. `plugin_init.py` entry point (`plugin_loaded()`).
6. `package-metadata.json` removed — not allowed by PC review rules.
7. `.no-sublime-package` removed — not allowed by PC review rules.
8. Contextual keymaps — every binding carries a `context` array.
9. `Main.sublime-menu` contains a `Key Bindings` item.
10. `CppVarHighlight.py` raw regex fix (file now removed as dead code).
11. Hidden console window fix (`cph_stress.py`) via `STARTUPINFO`.
12. Hidden console window fix (`Modules/ProcessManager.py`) via `STARTUPINFO`.
13. Syntax-specific settings (`TestSyntax.sublime-settings`).
14. Settings file is valid JSON with per-language `run_settings`.
15. Status consistency (`COMPILING`) in `test_manager.py`.
16. `ProcessManager.run_file` `is_run` guard fixed; `compile()` `stdin=None`
    (deadlock removed).

## Package Control Review Checklist (verified for v1.4.2)

- [x] No root-level plugin imports (`__init__.py` comment-only; `plugin_init.py` entry point).
- [x] Key bindings carry `context` arrays.
- [x] Empty/comment-only `__init__.py` at package root.
- [x] No `package-metadata.json`, no `.no-sublime-package`.
- [x] All subpackages have `__init__.py` (`core/`, `Modules/`, `Highlight/`).
- [x] Resources load from inside a `.sublime-package` (`sublime.load_resource`).
- [x] `messages.json` + `messages/install.txt` present.
- [x] `.gitattributes` excludes dev-only files from the package.
- [x] Commands and key bindings are listed in the menu and the command palette,
      and open settings in a split view.
- [x] No package reload required for settings changes (`Settings.add_on_change`).
- [x] Plugin code runs on the default Python 3.3 host (guarded by
      `tests/check_py33.py`).
- [x] No writes to the user's settings file from background callbacks.
- [x] `tests/`, `.github/` and dev documents are `export-ignore`d.
- [x] `README.md` and `LICENSE` included for users.

## Channel entry (repository/a.json)

The entry is filed under the letter of the published name (`a` for
"Algorithm Competition Assistant"), not the repository name. Package Control
derives the install folder from `name`, so the repository URL stays
`chenmoulaile/cph-by-chenkx`.

```json
{
	"name": "Algorithm Competition Assistant",
	"details": "https://github.com/chenmoulaile/cph-by-chenkx",
	"labels": ["c", "c++", "competitive programming", "testing"],
	"releases": [
		{
			"sublime_text": ">=4095",
			"tags": true
		}
	]
}
```

## Additional notes for reviewers

- `test_manager.py` compile callback handles `cmp_data` safely (`None` check
  before indexing).
- Memory limit verdicts are real: peak memory is sampled per platform
  (`Modules/memprobe.py`) instead of being dead configuration.
- Process termination kills the whole process tree on POSIX (`killpg`, SIGTERM
  → SIGKILL) and on Windows (`taskkill /F /T`).
- `Modules/` must stay inside the package: `test_edit.py` and `test_manager.py`
  import `ProcessManager` from it.
