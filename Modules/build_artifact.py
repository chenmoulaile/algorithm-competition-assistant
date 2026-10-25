"""Where the compiler *actually* put the binary.

The run command says the program is `A  中文.exe`, but on Windows the
compiler may create that file through the ANSI (GBK) codepage: the bytes it
writes on disk are the GBK encoding of the name, so the directory listing
shows `A  ¿\xadÉªÁÕµÄÎ¯ÍÐ·Ö×é.exe` and `os.path.exists('A  中文.exe')` is
False. Running the compiled program then failed with

    FileNotFoundError: [WinError 2] 系统找不到指定的文件

which surfaced as an empty output / "generator failed" / "file not found"
depending on which program it was. Scanning the directory for the file the
compiler just wrote fixes every one of those paths at once.
"""

import os
import re

#: `-o "some name.exe"` (quoted first: the name may contain spaces).
_OUT_FLAG_QUOTED = re.compile(r'-o\s+"([^"]+)"')
#: `-o name.exe`, unquoted.
_OUT_FLAG_BARE = re.compile(r'-o\s+([^\s"]+)')

#: Codepages a mangled name may have been encoded with.
_CODEPAGES = ('cp936', 'gbk', 'gb18030', 'cp950', 'big5', 'mbcs', 'latin-1')

#: Suffixes a run command may point at with a bare (not pathed) name.
_EXE_SUFFIXES = ('.exe', '.bin', '.out', '.run')


def output_path_from_compile_cmd(cmd):
    """The output path written by a compile command, or None.

    Only `-o` is recognised: that is what every shipped template and
    FastOlympicCoding-derived config uses for C/C++.
    """
    if not cmd:
        return None
    match = _OUT_FLAG_QUOTED.search(cmd)
    if match:
        return match.group(1)
    match = _OUT_FLAG_BARE.search(cmd)
    if not match:
        return None
    return match.group(1)


def isolate_output(cmd, out_dir):
    """Point the `-o` target of a compile command at `out_dir`.

    The stress test compiles the very source the user is compiling by hand.
    Writing - and then running - the same `.exe` made the file busy for the
    whole run, so rebuilding it failed with

        ld.exe: cannot open output file ... .exe: Permission denied

    and the user could not compile while a stress test was in progress.
    Building into a scratch directory removes the clash entirely.

    Returns (new_cmd, output_path); both are unchanged/None when the command
    has no `-o` at all (javac, interpreted languages).
    """
    if not cmd or not out_dir:
        return cmd, None
    wanted = output_path_from_compile_cmd(cmd)
    if not wanted:
        return cmd, None
    target = os.path.join(out_dir, os.path.basename(wanted))
    # A function replacement keeps Windows backslashes out of the escape
    # handling that a plain replacement string would apply to them.
    replacement = '-o "%s"' % target
    new_cmd, count = _OUT_FLAG_QUOTED.subn(lambda m: replacement, cmd, count=1)
    if not count:
        new_cmd, count = _OUT_FLAG_BARE.subn(lambda m: replacement, cmd, count=1)
    if not count:
        return cmd, None
    return new_cmd, target


def names_match(disk_name, wanted_name):
    """True when `disk_name` is `wanted_name` after a codepage round trip.

    `中文.exe` written through GBK and read back as latin-1 becomes
    `ÖÐÎÄ.exe`; both of the directions below are checked so it does not
    matter which side of the confusion we are looking at.
    """
    if not disk_name or not wanted_name:
        return False
    if disk_name == wanted_name or disk_name.lower() == wanted_name.lower():
        return True
    for encoding in _CODEPAGES:
        try:
            if wanted_name.encode(encoding).decode('latin-1') == disk_name:
                return True
        except (UnicodeError, LookupError):
            pass
        try:
            if disk_name.encode('latin-1').decode(encoding) == wanted_name:
                return True
        except (UnicodeError, LookupError):
            pass
    return False


def resolve_artifact(expected, src_dir=None, started_at=None):
    """Path of the file the compiler produced for `expected`, or None.

    Preference order: the expected path itself, a file whose name matches
    after a codepage round trip, then (only when `started_at` is known) the
    most recently modified candidate, i.e. the one this compile wrote.
    """
    if not expected:
        return None
    if os.path.exists(expected):
        return expected
    folder = os.path.dirname(expected) or src_dir or '.'
    if not os.path.isdir(folder):
        return None
    wanted = os.path.basename(expected)
    suffix = os.path.splitext(wanted)[1].lower()
    # Keep the separator style of the caller: run commands mix the two and a
    # Windows backslash inside a POSIX-quoted command looks like an escape.
    separator = '/' if ('/' in expected and '\\' not in expected) else os.sep
    folder = folder.rstrip('/\\')
    newest = None
    newest_time = -1.0
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in entries:
        if suffix and not name.lower().endswith(suffix):
            continue
        full = folder + separator + name
        if not os.path.isfile(full):
            continue
        if names_match(name, wanted):
            return full
        if started_at is None:
            continue
        try:
            stamp = os.path.getmtime(full)
        except OSError:
            continue
        if stamp + 1.0 < started_at:
            continue
        if stamp > newest_time:
            newest_time = stamp
            newest = full
    return newest


def _exists(path, src_dir):
    """os.path.exists, but resolving a bare name against src_dir too."""
    if os.path.exists(path):
        return True
    if not os.path.isabs(path) and src_dir:
        return os.path.exists(os.path.join(src_dir, path))
    return False


_ANNOUNCED = set()


def reset_announced():
    """Forget which binary-name mismatches were already reported.

    The set only ever grew, for the whole session: one entry per (actual,
    wanted) pair is small, but every stale name a retargeted build system
    ever produced stayed alive until the editor closed. The force-recompile
    hook clears it alongside the std-ladder reset - a fresh compile is also
    a fresh chance to hear the mismatch again.
    """
    _ANNOUNCED.clear()


def print_safe(text):
    """print() that survives a narrow console.

    Sublime's console is UTF-8, but a redirected one is not: the Windows CI
    runner is cp1252, and printing the name of a mangled binary there raised
    UnicodeEncodeError and failed the whole test run (v1.4.10). Fall back to
    an escaped ASCII rendering, which is more readable for these names anyway.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            print(text.encode('ascii', 'backslashreplace').decode('ascii'))
        except Exception:
            pass
    except Exception:
        pass


def _announce(actual, wanted):
    """Say it once per binary: a run happens once per test otherwise."""
    key = (actual, wanted)
    if key in _ANNOUNCED:
        return
    _ANNOUNCED.add(key)
    print_safe('[Algorithm Competition Assistant] the binary is %r on disk, not %r'
               % (os.path.basename(actual), os.path.basename(wanted)))


def _looks_like_path(token):
    """A bare command name (python, java, g++) must not be touched."""
    if not token:
        return False
    if os.path.isabs(token) or os.sep in token or '/' in token or '\\' in token:
        return True
    return os.path.splitext(token)[1].lower() in _EXE_SUFFIXES


def retarget_path(path, src_dir=None):
    """The path to actually run, when `path` is not on disk.

    Used by the stress test, which builds an argv list itself.
    """
    if not path or _exists(path, src_dir):
        return path
    resolved = resolve_artifact(path, src_dir or os.path.dirname(path))
    if resolved and resolved != path:
        _announce(resolved, path)
        return resolved
    return path


def retarget_command(cmd, src_dir=None):
    """Point a run command at the binary the compiler actually wrote.

    Only the executable token is touched, and only when it is missing: a
    normal command (or anything resolved through PATH, such as `python`)
    passes through unchanged.
    """
    if not cmd:
        return cmd
    match = re.match(r'\s*("[^"]*"|[^\s]+)', cmd)
    if not match:
        return cmd
    token = match.group(1)
    raw = token
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    if not raw or _exists(raw, src_dir) or not _looks_like_path(raw):
        return cmd
    resolved = resolve_artifact(raw, src_dir or os.path.dirname(raw))
    if not resolved or resolved == raw:
        return cmd
    _announce(resolved, raw)
    return '"%s"%s' % (resolved, cmd[match.end():])
