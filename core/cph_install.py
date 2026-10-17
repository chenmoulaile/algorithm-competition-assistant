"""Algorithm Competition Assistant - 安装形态自检

为什么需要这个：Package Control 把包当 zip 装在 `Installed Packages/`，而一次
开发用的散装目录安装放在 `Packages/`。PC 重新安装时会把 `Packages/<名>` 目录
**清空**（本机实测：2026-10-17 23:15 重启后目录还在、文件全没了），于是磁盘上
改好的代码不再运行，跑的却是旧 zip —— 症状是"我改的东西没生效"，而不是任何报错。

这里只做一件事：开机时把"我到底从哪儿加载的、还有没有另一份装着旧版本"讲出来，
并且把这个判断做成可测的纯函数（不依赖 Sublime 运行时，回归测试可直接调用）。
"""

import os

#: 一份安装里能代表整个包的文件；缺了它就不算一次有效安装
PROBE_FILE = 'cph_companion.py'


def _version_at(directory):
    """Read VERSION out of a package directory, without importing it."""
    try:
        with open(os.path.join(directory, 'plugin_init.py'), 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('VERSION'):
                    quote = stripped.find("'")
                    if quote < 0:
                        quote = stripped.find('"')
                    if quote < 0:
                        return ''
                    end = stripped.find(stripped[quote], quote + 1)
                    if end < 0:
                        return ''
                    return stripped[quote + 1:end]
    except Exception:
        return ''
    return ''


def _version_in_zip(path):
    """The VERSION inside a .sublime-package zip, or '' when unreadable."""
    try:
        import zipfile
        with zipfile.ZipFile(path) as archive:
            src = archive.read('plugin_init.py').decode('utf-8', 'replace')
    except Exception:
        return ''
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('VERSION'):
            quote = stripped.find("'")
            if quote < 0:
                quote = stripped.find('"')
            if quote < 0:
                return ''
            end = stripped.find(stripped[quote], quote + 1)
            return stripped[quote + 1:end] if end >= 0 else ''
    return ''


def install_mode(loaded_dir):
    """`'zip'` when the host loaded us out of a .sublime-package, else 'folder'."""
    name = os.path.basename(os.path.abspath(loaded_dir or ''))
    return 'zip' if name.endswith('.sublime-package') else 'folder'


def _folder_is_install(directory):
    return bool(directory) and os.path.isfile(os.path.join(directory, PROBE_FILE))


def _zip_path(packages_dir, name):
    return os.path.join(os.path.dirname(os.path.abspath(packages_dir)),
                        'Installed Packages', name + '.sublime-package')


def describe_installs(packages_dir, name, loaded_dir, loaded_version=''):
    """What is installed where, as lines fit for the console.

    `packages_dir` is sublime.packages_path(), `loaded_dir` the directory the
    host actually loaded us from. Returns [] when there is nothing to warn
    about - one install, in the shape the user asked for.
    """
    lines = []
    folder_dir = os.path.join(packages_dir, name)
    zip_file = _zip_path(packages_dir, name)

    folder_live = _folder_is_install(folder_dir)
    folder_emptied = (os.path.isdir(folder_dir) and not folder_live
                      and not os.listdir(folder_dir))
    zip_live = os.path.isfile(zip_file)
    base = os.path.basename(os.path.abspath(loaded_dir or '')).replace(
        '.sublime-package', '')

    if not folder_live and not zip_live:
        return lines

    # The smoking gun of a Package Control reinstall: the folder is there and
    # empty, and the version running is the zip's.
    if folder_emptied and zip_live:
        lines.append('%s: the folder install at %s is EMPTY - Package Control '
                     'replaced it with %s (version %s). Edits to that folder do '
                     'nothing; remove "%s" from installed_packages in '
                     'Packages/User/Package Control.sublime-settings to stop it '
                     'being reinstalled.'
                     % (name, folder_dir, os.path.basename(zip_file),
                        _version_in_zip(zip_file) or '?', name))
        return lines

    if folder_live and zip_live:
        lines.append('%s: both a folder install and %s exist; Sublime loads the '
                     'folder. The zip is %s, the folder is %s%s.'
                     % (name, os.path.basename(zip_file),
                        _version_in_zip(zip_file) or '?',
                        _version_at(folder_dir) or '?',
                        '' if base == name else ' (loaded from the zip anyway)'))
        return lines

    # Loaded from a place that is not the folder we expected.
    if base != name and base:
        lines.append('%s: loaded from %r, not from Packages/%s - check which '
                     'install is live (zip version %s).'
                     % (name, loaded_dir, name, _version_in_zip(zip_file) or '?'))
    return lines
