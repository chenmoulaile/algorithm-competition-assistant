# Package Control submission notes

Repository: https://github.com/chenmoulaile/algorithm-competition-assistant
Channel entry: `repository/a.json`
Submitted at: v2.4.11. Current version: v3.0.7.

This file records what a reviewer needs to know about the package layout, what has been
checked against the review guidelines, and how a release is published. User-facing changes
are in [RELEASE_NOTES_CN.md](RELEASE_NOTES_CN.md); the manual is [README_CN.md](README_CN.md)
and [README.md](README.md).

## What the package is

A test runner for competitive programming inside Sublime Text 4 (build 4095 or newer). It runs
the tests of the file being edited, judges them locally, and displays the result. It is a fork
of FastOlympicCoding, renamed and substantially changed; the fork is documented in both
READMEs.

There are no binaries and no third-party dependencies. Network access is limited to two
optional features: the local Competitive Companion listener on `127.0.0.1:12345`, which can be
turned off (`companion_auto_listen`) and stopped at runtime, and the problem fetch command,
which makes one HTTP request to the URL the user supplies.

## Layout

- Root `.py` files are plugin entry points. Sublime loads every `.py` in the package root, so
  they may not import each other; shared code lives in `core/`, `Modules/` and `Highlight/`.
  `__init__.py` at the root is comment-only, and `plugin_init.py` is the entry point with
  `plugin_loaded()` / `plugin_unloaded()`.
- `core/` holds settings, i18n, verdict rules, process-adjacent logic and the feature modules.
- `Modules/ProcessManager.py` is imported by `test_manager.py` and `test_edit.py`, so the
  directory has to ship inside the package.
- `Highlight/` holds the minihtml templates and the stylesheet. They are read with
  `sublime.load_resource()`, never with `open()`, because the package may be a zip.
- `Default (<platform>).sublime-keymap` carries the shipped bindings. There is one keymap per
  platform and no copy in `Example` files.
- `.gitattributes` marks `tests/`, `.github/` and the development notes as `export-ignore`.

## Review checklist

Checked against the Package Control review guidelines, and enforced by tests where possible:

- [x] No root-level plugin imports; `__init__.py` is comment-only; one entry point.
- [x] Every shipped key binding has a `context`; code-file bindings also honour
      `enable_keybindings`.
- [x] No `package-metadata.json`, no `.no-sublime-package`.
- [x] Every subpackage has `__init__.py`, so a zip install loads them.
- [x] Resources load from inside a `.sublime-package` (no `open()` on package files).
- [x] `messages.json` and `messages/install.txt` are present; every key is `install` or a
      three-part version.
- [x] Commands appear in the command palette and in `Main.sublime-menu`.
- [x] Settings and key bindings open through `edit_settings` in a split view.
- [x] Settings changes need no package reload (`Settings.add_on_change`).
- [x] The code runs on the Python 3.3 plugin host; `tests/check_py33.py` checks for f-strings,
      `subprocess.run`, `Popen(encoding=...)` and similar.
- [x] Nothing writes the user's settings file from a background callback.
- [x] `subprocess.Popen` calls pass `startupinfo` on Windows, so no console window flashes.
- [x] `tests/`, `.github/` and development documents are excluded from the package.
- [x] README and LICENSE ship with the package.

Current test status: `python tests/run_tests.py` reports 1044 checks, 0 failures. The suite
uses a fake `sublime` module; CI runs it on Linux (3.9 and 3.12), Windows and macOS, plus
`check_py33.py`, `check_messages.py`, `check_import_dir.py` and `check_perf.py`.

## Paths and the two install layouts

Package Control installs `Installed Packages/<Name>.sublime-package`, a zip. A folder install
puts a directory under `Packages/`. Both have to work, and most packaging bugs in this
project came from handling only one of them:

- v1.4.0: templates were read with `open()`, which fails inside a zip. Fixed with
  `sublime.load_resource()`.
- v1.4.1: the plugin host is Python 3.3; `Popen(encoding=...)` broke every run.
- v2.4.9: menu entries passed a `Packages/<pkg>/<file>` resource path to `edit_settings`,
  which expects an OS path. Fixed with an absolute path under `Packages/`.
- v2.4.11: that absolute path kept the `.sublime-package` suffix as the package name, so
  every resource lookup failed in a zip install. The name is now derived once with the suffix
  stripped.
- v2.5.0: installing or upgrading the zip empties a same-named folder in `Packages/`, which
  silently discarded a source checkout. Startup now warns, and the environment check reports
  whether the loaded copy is a folder or a zip.
- v2.9.1: `edit_settings` cannot open a path inside a zip at all. Settings, key bindings,
  TestSyntax settings and the READMEs now use `${packages}/<name>/<file>`, which both layouts
  resolve.

The rule that came out of this: never build a path from the package's display name, and never
assume the package is a directory. `core/cph_settings.py` has one package-name constant and
one path helper; everything else goes through them.

## Review feedback that changed the package

- v1.4.7: root-level modules imported each other; some bindings had no context; a
  platform-specific settings file was shipped under a mismatched name; the channel entry had a
  redundant `name`.
- v1.4.12: the package claimed 14 keys used for code editing. They moved to
  `Example (<platform>).sublime-keymap` as suggestions. The context menu became conditional
  and was cut to six entries; settings and key bindings moved to `edit_settings`.
- v1.4.13: a menu entry pointed at `${packages}/Default/...`; `base_file` has to live in the
  submitted package.
- v1.4.17: bindings had been copied into the user's own keymap, where they survived
  uninstall. They moved back into the package keymap behind a context gate.
- v2.1.1: `messages.json` used `"2.0"`, which is not a three-part version; three `Popen` calls
  did not hide the console window.
- v2.4.4: the published name became Algorithm Competition Assistant. Settings file, palette
  captions, menu entries and `${packages}` paths follow it. User data files keep their old
  names on purpose.
- v2.4.11: the repository was renamed to match the display name. GitHub redirects the old URL;
  the merged channel entry should be updated when convenient.

## Channel entry

The entry is filed under the first letter of the published name, in `repository/a.json`:

```json
{
	"name": "Algorithm Competition Assistant",
	"details": "https://github.com/chenmoulaile/algorithm-competition-assistant",
	"labels": ["c", "c++", "competitive programming", "testing"],
	"releases": [
		{
			"sublime_text": ">=4095",
			"tags": true
		}
	]
}
```

`>=4095` is required: the card styling uses ST4 minihtml CSS variables
(`var(--foreground)`, `color(... alpha(...))`).

## Releasing

Package Control takes the version from git tags, not from the default branch and not from
GitHub Releases. packagecontrol.io crawls the repository, turns version-like tags into channel
releases and publishes `channel_v3.json`; Sublime upgrades from that file. A commit that is
never tagged is invisible: v3.0.0 through v3.0.3 were merged to `main` without tags and users
stayed on 2.9.2 until v3.0.4 was tagged.

The order matters:

1. Bump `VERSION` in `plugin_init.py`, add `messages/<version>.txt`, add the key to
   `messages.json`. `tests/check_messages.py` fails if the three disagree.
2. Commit and push.
3. Tag the commit and push the tag: `git tag vX.Y.Z <sha> && git push origin vX.Y.Z`, or
   `gh release create`. The tag must parse as a version, for example `v3.0.7`.

packagecontrol.io needs one crawl before the channel lists the version (the "Last Seen" field
on the package page is that crawl). After that, users get it through
*Package Control: Upgrade Package*.
