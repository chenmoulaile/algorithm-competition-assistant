# -*- coding: utf-8 -*-
"""Cross-check the release notes against messages.json, in both directions.

    python tests/check_messages.py

It lives beside run_tests.py and check_py33.py on purpose: those are the two
scripts CI runs, and .workbuddy/ is `export-ignore`d in .gitattributes and
skipped by publish_api.py, so a step pointing into it would fail with
"no such file or directory" on every runner.

Why this exists
---------------
tests/run_tests.py only checks the one direction "every file named in
messages.json exists". That passes forever even when a published version has
no upgrade message at all, which is exactly how v2.1.1 and v1.4.7 shipped:
both have a tag, a GitHub release and a section in RELEASE_NOTES_CN.md, and
neither has a messages/ file or a messages.json key.

The other tempting check - "messages/ minus the files messages.json names" -
is useless here: messages.json and messages/ agree with each other on both
missing versions, so the difference is empty and the script is green forever.
The only source that knows about a version independently is the release
notes, so that is what the comparison is anchored on:

    RELEASE_NOTES_CN.md  "# vX.Y.Z" headings   vs   messages.json keys

Everything older than MIN_VERSION predates the message file mechanism (and the
notes themselves jump from 1.4.0 back to 1.2.2), so those are whitelisted.

Exit code 0 when both sides agree, 1 when a version is missing on either side
or a key/file mapping is broken.
"""
import json
import os
import re
import sys

# tests/check_messages.py -> the checkout it belongs to.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES = os.path.join(ROOT, 'RELEASE_NOTES_CN.md')
MESSAGES_JSON = os.path.join(ROOT, 'messages.json')
MESSAGES_DIR = os.path.join(ROOT, 'messages')

# Versions older than this have no message files at all, by design.
MIN_VERSION = (1, 4, 5)

# Release notes heading -> messages.json key, when the two spell a version
# differently on purpose. v2.0 was tagged and released as "v2.0" but Package
# Control rejects a two-component version, so the key is "2.0.0" (v2.1.1 in
# RELEASE_NOTES_CN.md is about that very fix). Without this entry the check
# would report 2.0 as a missing key on every run.
VERSION_ALIASES = {
    '2.0': '2.0.0',
}

# messages.json key -> message file, when the file is not named after the key.
# Same case as above: the 2.0 upgrade message was written as messages/2.0.txt
# and reused as-is instead of being renamed.
FILE_ALIASES = {
    '2.0.0': 'messages/2.0.txt',
}

# "# v1.4.19", "# v2.0", and (once) "# v1.0.6 - v1.0.7".
HEADING_RE = re.compile(r'^#\s+v([0-9][0-9A-Za-z.+-]*)')
KEY_RE = re.compile(r'^\d+\.\d+\.\d+$')


def version_tuple(text):
    """'1.4.10' -> (1, 4, 10). Returns None when it is not a version."""
    parts = text.split('.')
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def covered(version):
    """True when `version` is new enough to need a message file."""
    return version is not None and version >= MIN_VERSION


def notes_versions():
    """Set of versions that have a '# vX' section, in file order."""
    found = []
    with open(NOTES, encoding='utf-8') as f:
        for line in f:
            m = HEADING_RE.match(line)
            if m:
                found.append(m.group(1))
    return found


def load_messages_json():
    with open(MESSAGES_JSON, encoding='utf-8') as f:
        return json.load(f)


def message_files():
    names = set()
    for name in os.listdir(MESSAGES_DIR):
        if name.endswith('.txt'):
            names.add('messages/' + name)
    return names


def main():
    for path in (NOTES, MESSAGES_JSON, MESSAGES_DIR):
        if not os.path.exists(path):
            print('missing %s' % os.path.relpath(path, ROOT))
            return 1

    headings = notes_versions()
    messages = load_messages_json()
    keys = set(k for k in messages if k != 'install')
    files = message_files()
    problems = []

    # --- every key must name a file that is really there -------------------
    for key in sorted(keys):
        target = messages[key]
        full = os.path.join(ROOT, target)
        if not os.path.isfile(full):
            problems.append('messages.json key "%s" points at %s, which does '
                            'not exist' % (key, target))

    # --- every message file must be reachable from messages.json -----------
    for target in sorted(files - set(messages.values())):
        problems.append('%s is not referenced by any messages.json key' % target)

    # --- the filename is expected to follow the key, except where aliased --
    for key in sorted(keys):
        target = messages[key]
        expected = 'messages/%s.txt' % key
        if target != expected and FILE_ALIASES.get(key) != target:
            problems.append('messages.json key "%s" names %s; expected %s '
                            '(or an entry in FILE_ALIASES)'
                            % (key, target, expected))

    # --- the two-way difference: notes headings vs messages.json keys ------
    for heading in headings:
        key = VERSION_ALIASES.get(heading, heading)
        if covered(version_tuple(key)) and key not in keys:
            problems.append('RELEASE_NOTES_CN.md has a "%s" section but '
                            'messages.json has no "%s" key - add '
                            'messages/%s.txt and the key'
                            % (heading, key, key))
    for key in sorted(keys):
        if not covered(version_tuple(key)):
            continue
        wanted = key
        for heading, alias in VERSION_ALIASES.items():
            if alias == key:
                wanted = heading
        if wanted not in headings:
            problems.append('messages.json has a "%s" key but '
                            'RELEASE_NOTES_CN.md has no "# v%s" section' % (key, wanted))

    print('release notes  : %d sections (%s .. %s)'
          % (len(headings), headings[-1], headings[0]))
    print('messages.json  : %d version keys' % len(keys))
    print('messages/      : %d files' % len(files))
    print('compared       : %s and newer (aliases: %s)'
          % ('.'.join(str(p) for p in MIN_VERSION),
             ', '.join('%s -> %s' % kv for kv in sorted(VERSION_ALIASES.items()))
             or '(none)'))
    if problems:
        print('%d problem(s):' % len(problems))
        for p in problems:
            print('  - %s' % p)
        return 1
    print('release notes and messages.json agree on every version')
    return check_plugin_version(keys)


def _version_key(version):
    """Sort key that reads '2.4.10' as newer than '2.4.9'.

    Numeric components, with any non-numeric tail (never expected here, but a
    'v' prefix or an 'rc1' would crash int()) compared as text so the sort is
    total and never raises.
    """
    parts = []
    for chunk in str(version).split('.'):
        digits = ''
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append((int(digits) if digits else -1, chunk))
    return tuple(parts)


def check_plugin_version(keys):
    """`plugin_init.VERSION` must be the newest key in messages.json.

    The startup banner prints it (plugin_init.plugin_loaded), so a stale
    constant would make the console name a build the host never loaded -
    precisely the confusion that banner exists to prevent. Lives in this file
    because this is the one that already owns the release bookkeeping.
    """
    path = os.path.join(ROOT, 'plugin_init.py')
    with open(path, encoding='utf-8') as f:
        source = f.read()
    m = re.search(r"^VERSION = '([^']+)'", source, flags=re.M)
    if not m:
        print('plugin_init.py declares no VERSION - the startup banner would '
              'name nothing')
        return 1
    declared = m.group(1)
    # Numeric, not lexical: max(['2.4.9', '2.4.10']) is '2.4.9', so a string
    # compare would have this guard insist the banner names an older build the
    # moment a two-digit patch number ships.
    newest = max(keys, key=_version_key)
    if declared != newest:
        print('plugin_init.VERSION is %s but messages.json newest is %s - '
              'the banner would name a build that was never loaded'
              % (declared, newest))
        return 1
    print('plugin_init    : VERSION %s matches messages.json' % declared)
    return 0


if __name__ == '__main__':
    sys.exit(main())
