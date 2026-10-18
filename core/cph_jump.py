"""Algorithm Competition Assistant - 把输出里的 `文件:行:列` 变成可点的跳转链接

运行错误的位置（`find_crash_location` 解析出的 `main.cpp:12`）和编译器诊断都
带着 `文件:行:列`，以前只是纯文本。这里把它们变成 minihtml 链接，点一下直接
跳到那一行。

minihtml 的链接回调只会拿到 href 原文，所以目标列表存在**视图设置**
`cph_jump_targets`（`[[绝对路径, 行号], ...]`）里，href 只带下标；运行面板和
详情视图各存自己的一份，互不干扰。

放在 core/ 是因为根级插件模块之间不允许互相 import（Package Control 审查规则）。
"""

import os
import re

import sublime


# g++ / clang:   main.cpp:12:5: error: 'x' was not declared in this scope
#                D:/a/main.cpp:12:5: fatal error: ...
#                main.cpp:12: error: ...            (no column)
# MSVC:          main.cpp(12,5): error C2065: ...
_GCC = re.compile(
    r'^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*'
    r'(?P<kind>fatal error|error|warning|note)\b\s*:?\s*(?P<msg>.*)$')
_MSVC = re.compile(
    r'^(?P<file>.+?)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*'
    r'(?P<kind>error|warning|note)\s*[A-Z]*\d*\s*:?\s*(?P<msg>.*)$')

TARGETS_KEY = 'cph_jump_targets'


def esc(s):
    """Escape for minihtml (it renders real markup)."""
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def diagnostics(text, limit=20):
    """Parse compiler output into [(file, line, col, kind, message)].

    Only the first `limit` entries survive: one template error produces
    hundreds of follow-up notes and the panel has to stay usable.
    """
    found = []
    for raw in (text or '').split('\n'):
        line = raw.strip()
        if not line:
            continue
        match = _GCC.match(line) or _MSVC.match(line)
        if not match:
            continue
        try:
            line_no = int(match.group('line'))
        except (TypeError, ValueError):
            continue
        col = match.group('col')
        found.append((
            match.group('file').strip(),
            line_no,
            int(col) if col else 0,
            match.group('kind'),
            match.group('msg').strip(),
        ))
        if len(found) >= limit:
            break
    return found


def resolve(path, base_dir=None):
    """Absolute, normalised path.

    Compiler output is normally absolute (the default compile_cmd passes
    "{source_file}"), but a diagnostic inside a quoted include can be relative
    to the source directory.
    """
    if not path:
        return path
    if base_dir and not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    try:
        return os.path.normpath(path)
    except Exception:
        return path


def set_targets(view, targets):
    """Store the jump targets on the view (the href only carries an index)."""
    view.settings().set(TARGETS_KEY, targets)


def links_html(entries, base_dir=None):
    """minihtml for a list of diagnostics; returns (html, targets)."""
    targets = []
    parts = []
    for path, line, col, kind, message in entries:
        path = resolve(path, base_dir)
        index = len(targets)
        targets.append([path, line])
        label = '%s:%d' % (os.path.basename(path), line)
        if col:
            label += ':%d' % col
        parts.append(
            '<a class="diag diag-%s" href="jump:%d">%s</a>'
            '<span class="diag-msg">%s</span>'
            % (kind.replace(' ', '-'), index, esc(label), esc(message[:160])))
    return '<div class="diag-list">' + '<br>'.join(parts) + '</div>', targets


def one_link(path, line, label=None):
    """Same, for a single location (the RE position of one test)."""
    path = resolve(path)
    targets = [[path, line]]
    text = label or ('%s:%d' % (os.path.basename(path or ''), line))
    html = ('<div class="diag-list">'
            '<a class="diag" href="jump:0">\u2197 %s</a></div>' % esc(text))
    return html, targets


def jump_to(view, target):
    """Handle a `jump:N` event for this view. Returns True when it jumped.

    A negative target counts from the end, so "the last diagnostic" is
    addressable without the caller knowing how many there were.
    """
    targets = view.settings().get(TARGETS_KEY) or []
    try:
        index = int(target)
    except (TypeError, ValueError):
        return False
    if index < 0:
        index += len(targets)
    if not (0 <= index < len(targets)) or not targets[index]:
        return False
    path = targets[index][0]
    try:
        line = int(targets[index][1] or 1)
    except (TypeError, ValueError):
        line = 1

    window = view.window()
    if window is None or not path:
        return False

    # Reuse an already-open tab so the user's layout and unsaved changes
    # survive the jump; only fall back to open_file when it is not open.
    wanted = os.path.normcase(path)
    for candidate in window.views():
        name = candidate.file_name()
        if name and os.path.normcase(os.path.normpath(name)) == wanted:
            window.focus_view(candidate)
            goto_line(candidate, line)
            return True
    window.open_file('%s:%d' % (path, line), sublime.ENCODED_POSITION)
    return True


def goto_line(view, line):
    try:
        point = view.text_point(max(0, line - 1), 0)
        view.sel().clear()
        view.sel().add(sublime.Region(point))
        view.show_at_center(point)
    except Exception:
        pass


def handle_event(view, event):
    """Shared phantom callback: `jump:N` -> jump, anything else -> ignored."""
    if isinstance(event, str) and event.startswith('jump:'):
        return jump_to(view, event[5:])
    return False
