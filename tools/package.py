#!/usr/bin/env python3
"""Build the .sublime-package zip, and optionally install it into Sublime.

Package Control ships `git archive` of the tag: .gitattributes decides what is
left out (tests/, promo/, RELEASE_NOTES_CN.md, tools/ ...), so this script asks
git for the same bytes instead of keeping a second, hand-written include list
that drifts from the repo.

    python tools/package.py                  # build dist/<name>.sublime-package
    python tools/package.py --install        # build, then replace the installed copy
    python tools/package.py --install --no-backup

The installed copy is what Sublime actually loads. Replacing it while Sublime
is running changes nothing until the app restarts: the plugin host has already
imported the modules from the old zip.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_NAME = 'Algorithm Competition Assistant'


def plugin_version():
    """VERSION from plugin_init.py - the single source of truth."""
    with open(os.path.join(ROOT, 'plugin_init.py'), encoding='utf-8') as handle:
        found = re.search(r"^VERSION = '([^']+)'", handle.read(), re.M)
    if not found:
        raise SystemExit('plugin_init.py has no VERSION line')
    return found.group(1)


def git_archive(destination):
    """Tracked files, minus everything .gitattributes marks export-ignore."""
    subprocess.check_call(
        ['git', 'archive', '--format=zip', '--prefix=', '-o', destination, 'HEAD'],
        cwd=ROOT)


def metadata(version):
    """The file Package Control writes on install, for this build.

    Written with the version this zip actually carries: without it Package
    Control has no version to compare against the channel and will happily
    reinstall the published one over a local build.
    """
    return {
        'name': PACKAGE_NAME,
        'version': version,
        'sublime_text': '>=4095',
        'platforms': ['*'],
        'python_version': '3.3',
        'url': 'https://github.com/chenmoulaile/algorithm-competition-assistant',
        'issues': 'https://github.com/chenmoulaile/algorithm-competition-assistant/issues',
        'author': ['chenmoulaile'],
        'description': u'\u4e00\u4e2a\u57fa\u4e8eFastOlympicCoding\u63d2\u4ef6\u7684\u4f18\u5316\u63d2\u4ef6\uff0c\u9002\u7528\u4e8eSublime Text',
        'labels': ['c', 'c++', 'competitive programming', 'testing'],
        'libraries': [],
        'release_time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
    }


def repack(source, destination, version):
    """Re-write the archive without directory entries, with metadata added."""
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as new:
        for info in old.infolist():
            if info.filename.endswith('/'):
                continue  # Package Control ships no directory entries
            new.writestr(info.filename, old.read(info.filename))
        new.writestr('package-metadata.json',
                     json.dumps(metadata(version), ensure_ascii=False))


def installed_dir():
    """Installed Packages of the Sublime Text the user runs."""
    candidates = []
    if os.environ.get('SUBLIME_TEXT_APPDATA'):
        candidates.append(os.environ['SUBLIME_TEXT_APPDATA'])
    if os.name == 'nt' and os.environ.get('APPDATA'):
        candidates.append(os.path.join(os.environ['APPDATA'], 'Sublime Text'))
    candidates.append(os.path.join(os.path.expanduser('~'), '.config', 'sublime-text'))
    candidates.append(os.path.join(os.path.expanduser('~'),
                                   'Library', 'Application Support', 'Sublime Text'))
    for base in candidates:
        path = os.path.join(base, 'Installed Packages')
        if os.path.isdir(path):
            return path
    return None


def install(built, backup=True):
    target_dir = installed_dir()
    if target_dir is None:
        raise SystemExit('could not find Installed Packages; pass --target')
    target = os.path.join(target_dir, PACKAGE_NAME + '.sublime-package')
    if os.path.exists(target) and backup:
        stamp = time.strftime('%Y%m%d-%H%M')
        shutil.copy2(target, target + '.bak-' + stamp)
        print('backed up  %s.bak-%s' % (os.path.basename(target), stamp))
    shutil.copy2(built, target)
    print('installed  %s' % target)
    print('restart Sublime Text to load it')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true',
                        help='copy the build into Installed Packages')
    parser.add_argument('--no-backup', action='store_true',
                        help='do not keep a .bak-<stamp> of the installed zip')
    parser.add_argument('--out', help='output path (default dist/<name>.sublime-package)')
    args = parser.parse_args()

    version = plugin_version()
    out = args.out or os.path.join(ROOT, 'dist', PACKAGE_NAME + '.sublime-package')
    os.makedirs(os.path.dirname(out), exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, 'raw.zip')
        git_archive(raw)
        repack(raw, out, version)

    with zipfile.ZipFile(out) as built:
        count = len(built.namelist())
    print('built      %s' % out)
    print('version    %s  (%d files)' % (version, count))

    if args.install:
        install(out, backup=not args.no_backup)


if __name__ == '__main__':
    main()
