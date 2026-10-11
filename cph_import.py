"""
Algorithm Competition Assistant - 从文件导入测试数据

支持四种来源:
1. in/out 文件对 (自动探测同名 .out/.ans 或 out.txt 等)
2. 单个文件 (内含 output 分隔行, 自动切分输入/输出)
3. cph-ng 风格 (同目录 in.txt / input.txt + 对应输出)
4. 文件夹批量导入 (所有 .in/.out 文件对)

注意: 所有命令通过 cph_view_tester 走正常 Run 流程刷新测评视图,
绝不直接对源代码视图调用 make_opd(load_session=True)。
"""

import sublime
import sublime_plugin
import os
import re
import glob

from .core.cph_settings import save_tests, load_all_tests
from .core.cph_tests_merge import merge_tests, merge_into_file
from .core.cph_i18n import t


def _strip_input_suffix(stem):
    """"1.in" -> "1" so "1.out.txt" pairs correctly with "1.in.txt"."""
    return re.sub(r'(?i)\.in$', '', stem)


def _is_dir(path):
    """True when the path is an existing directory.

    `os.path.exists()` says True for a directory too, and a directory
    being "there" is not the same as a data file being there.
    """
    try:
        return os.path.isdir(path)
    except Exception:
        return False


def _is_file_like(path):
    """True for a regular file, or a symlink/hard link to one.

    Directories are excluded explicitly: `[Errno 13] Permission denied`
    is what Windows raises when open() is handed a directory.
    """
    try:
        if os.path.isdir(path):
            return False
        return os.path.isfile(path)
    except Exception:
        return False


def _read_text(file_path):
    """Read test data tolerating non-UTF-8 encodings.

    Chinese OJ data is often GB2312/GBK; strict UTF-8 decoding used to
    abort the whole import with an exception.

    Passing a DIRECTORY used to surface as Windows'
    `[Errno 13] Permission denied: '<dir>'`, which reads like a real
    permissions problem instead of "this is a folder". Explorer's
    "Copy as path" produces a folder path just as happily as a file
    path, so all four import flows could be fed one. Detected here, at
    the single reading chokepoint, with a message that says what to do.
    """
    if not file_path or _is_dir(file_path):
        # str(e) of the old PermissionError was the raw errno 13 text;
        # this says what actually went wrong.
        raise IOError('%s is a directory, not a test data file - point at '
                      'a .in/.out file, or use "import from folder"'
                      % (file_path or '(empty path)'))
    # A UTF-16 BOM must be handled before the UTF-8 attempt: UTF-16 bytes
    # decode "successfully" as UTF-8 (mojibake) or fall through to gb18030,
    # so the utf-16 branch below could never actually be reached.
    with open(file_path, 'rb') as fb:
        head = fb.read(2)
    if head in (b'\xff\xfe', b'\xfe\xff'):
        with open(file_path, 'r', encoding='utf-16') as f:
            return f.read()

    errors = []
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            with open(file_path, 'r', encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError as e:
            errors.append(str(e))
        except Exception:
            raise
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


from .core.cph_target import visible as context_menu_visible


class CphImportTestsCommand(sublime_plugin.TextCommand):
    def is_visible(self, event=None, **kwargs):
        """Context menu: only offered where this package applies.

        A .sublime-menu item has no `context` key in Sublime Text, so the
        command decides (Default's own `open_context_url` does the same).
        """
        return context_menu_visible(self.view, event)

    def run(self, edit):
        window = self.view.window()
        if not window:
            return

        options = [
            [t('import_from_pair'), t('import_from_pair_desc')],
            [t('import_from_single'), t('import_from_single_desc')],
            [t('import_from_cphng'), t('import_from_cphng_desc')],
            [t('import_from_folder'), t('import_from_folder_desc')],
        ]

        def on_select(idx):
            if idx == 0:
                self._import_from_pair(window)
            elif idx == 1:
                self._import_from_single(window)
            elif idx == 2:
                self._import_from_cphng(window)
            elif idx == 3:
                self._import_from_folder(window)

        window.show_quick_panel(options, on_select)

    # ---------- 1. in/out pair ----------

    def _import_from_pair(self, window):
        src_file = self.view.file_name()
        if not src_file:
            sublime.status_message(t('save_file_first'))
            return

        src_dir = os.path.dirname(src_file)

        def on_done(input_path):
            if not input_path:
                return
            if _is_dir(input_path):
                sublime.error_message(t('path_is_directory', path=input_path))
                return
            candidates = self._find_output_for_input(input_path)
            if not candidates:
                sublime.status_message(t('output_file_not_found'))
                self._import_input_only(input_path)
                return

            if len(candidates) == 1:
                self._do_import(input_path, candidates[0], append=True)
            else:
                window.show_quick_panel(
                    [[os.path.basename(c)] for c in candidates],
                    lambda idx: idx >= 0 and self._do_import(input_path, candidates[idx], append=True)
                )

        def ask_for_path():
            window.show_input_panel(
                t('input_file_path') + ':', '',
                lambda s: on_done(s.strip() or None),
                None, None
            )

        existing_in = self._find_cphng_input(src_dir)
        if existing_in:
            def on_pick(idx):
                # idx == 1 is "choose another file": the old code called
                # on_done(None), which silently did nothing at all.
                if idx == 0:
                    on_done(existing_in)
                elif idx == 1:
                    ask_for_path()

            window.show_quick_panel(
                [[os.path.basename(existing_in), t('use_existing_in_cphng')],
                 [t('choose_other_file')]],
                on_pick
            )
        else:
            ask_for_path()

    def _find_cphng_input(self, src_dir):
        candidates = ['in.txt', 'input.txt', 'stdin.txt']
        for c in candidates:
            p = os.path.join(src_dir, c)
            if os.path.exists(p):
                return p
        return None

    def _find_output_for_input(self, input_path):
        candidates = []
        base, _ext = os.path.splitext(input_path)
        # "1.in.txt" -> stem "1": also try 1.out / 1.ans next to it
        stems = {base, _strip_input_suffix(base)}
        for stem in stems:
            for new_ext in ['.out', '.ans', '.out.txt', '.ans.txt', '.txt']:
                cand = stem + new_ext
                if _is_file_like(cand) and cand != input_path:
                    candidates.append(cand)
        for new_name in ['out.txt', 'output.txt', 'stdout.txt', 'ans.txt']:
            cand = os.path.join(os.path.dirname(input_path), new_name)
            if _is_file_like(cand) and cand != input_path:
                candidates.append(cand)
        return sorted(set(candidates))

    def _import_input_only(self, input_path):
        try:
            content = _read_text(input_path)
            tests = [{'test': content, 'correct_answers': []}]
            self._save_and_show(tests, input_path, append=True)
        except Exception as e:
            sublime.error_message(t('import_failed', error=str(e)))

    def _do_import(self, input_path, output_path, append=False):
        try:
            inp = _read_text(input_path)
            out = ''
            if output_path:
                out = _read_text(output_path)
            tests = [{'test': inp, 'correct_answers': [out.strip()] if out.strip() else []}]
            self._save_and_show(tests, input_path, append=append)
        except Exception as e:
            sublime.error_message(t('import_failed', error=str(e)))

    # ---------- 2. single file with separator ----------

    def _import_from_single(self, window):
        src_file = self.view.file_name()
        if not src_file:
            sublime.status_message(t('save_file_first'))
            return

        window.show_input_panel(
            t('import_file_path') + ':', '',
            lambda s: self._do_import_single(s.strip() or None),
            None, None
        )

    def _do_import_single(self, file_path):
        if not file_path:
            return
        # Windows reports "Permission denied" for a directory handed to
        # open(); say what actually happened before the read attempt.
        if _is_dir(file_path):
            sublime.error_message(t('path_is_directory', path=file_path))
            return
        if not os.path.exists(file_path):
            sublime.status_message(t('file_not_found'))
            return

        try:
            content = _read_text(file_path)
        except Exception as e:
            sublime.error_message(t('read_failed', error=str(e)))
            return

        sep_patterns = [
            (r'\n[-=]{3,}\s*output\s*[-=]{0,}\n', 'output'),
            (r'\n#{1,3}\s*output\s*#{0,}\n', 'output'),
            (r'\n-{3,}\s*OUT(?:PUT)?\s*-{0,}\n', 'OUT'),
            (r'\noutput\s*:\s*\n', 'output:'),
        ]

        for pattern, _ in sep_patterns:
            match = re.split(pattern, content, maxsplit=1, flags=re.IGNORECASE)
            if len(match) == 2:
                inp, out = match[0].strip(), match[1].strip()
                tests = [{'test': inp + '\n', 'correct_answers': [out] if out else []}]
                self._save_and_show(tests, file_path, append=True)
                return

        tests = [{'test': content, 'correct_answers': []}]
        self._save_and_show(tests, file_path, append=True)

    # ---------- 3. cph-ng style ----------

    def _import_from_cphng(self, window):
        src_file = self.view.file_name()
        if not src_file:
            sublime.status_message(t('save_file_first'))
            return

        src_dir = os.path.dirname(src_file)

        in_path = self._find_cphng_input(src_dir)
        if not in_path:
            sublime.error_message(t('no_cphng_files_found'))
            return

        candidates = self._find_output_for_input(in_path)
        if not candidates:
            self._import_input_only(in_path)
            return

        if len(candidates) == 1:
            self._do_import(in_path, candidates[0], append=True)
        else:
            window.show_quick_panel(
                [[os.path.basename(c)] for c in candidates],
                lambda idx: idx >= 0 and self._do_import(in_path, candidates[idx], append=True)
            )

    # ---------- 4. folder batch import ----------

    def _import_from_folder(self, window):
        src_file = self.view.file_name()
        if not src_file:
            sublime.status_message(t('save_file_first'))
            return

        src_dir = os.path.dirname(src_file)
        window.show_input_panel(
            t('import_folder_path') + ':', src_dir,
            lambda s: self._do_import_from_folder(s.strip(), window),
            None, None
        )

    def _do_import_from_folder(self, selected_path, window):
        """Import all .in/.out pairs from a folder; merges (dedup) with existing tests."""
        if not selected_path:
            return
        # A data FILE pasted here used to reach open() and surface as
        # `[Errno 13] Permission denied` on Windows (a file that is not a
        # directory is refused with the same errno in reverse). Say so.
        if not os.path.isdir(selected_path):
            if os.path.exists(selected_path):
                sublime.error_message(t('path_is_file_not_dir', path=selected_path))
            else:
                sublime.status_message(t('file_not_found'))
            return

        in_files = []
        for pattern in ['*.in', '*.in.txt']:
            in_files.extend(glob.glob(os.path.join(selected_path, pattern)))

        if not in_files:
            sublime.error_message(t('no_test_files_in_dir'))
            return

        in_files.sort()

        new_tests = []
        skipped = []
        for in_file in in_files:
            # ".in.txt" must lose BOTH suffixes, otherwise the base
            # becomes "1.in" and no "1.out.txt" is ever found.
            base_file = _strip_input_suffix(os.path.splitext(in_file)[0])
            out_file = None
            for out_ext in ['.out', '.ans', '.ans.txt', '.out.txt']:
                candidate = base_file + out_ext
                if _is_file_like(candidate):
                    out_file = candidate
                    break

            try:
                inp = _read_text(in_file)
                out = ''
                if out_file:
                    out = _read_text(out_file).strip()

                new_tests.append({'test': inp, 'correct_answers': [out] if out else []})
            except Exception as e:
                # Keep going: one unreadable pair must not abort the batch,
                # but the user has to be able to see WHY the count is low -
                # a console-only print() left a silently truncated import
                # looking like success.
                skipped.append('%s (%s)' % (os.path.basename(in_file), e))
                print('[Algorithm Competition Assistant] Failed to import %s: %s' % (in_file, e))

        if new_tests:
            # "3 of 5 imported, 2 skipped" beats "imported 3 tests" when a
            # sample is locked or oversized.
            if skipped:
                sublime.status_message(t('import_partially_done',
                                          kept=len(new_tests),
                                          total=len(new_tests) + len(skipped)))
            self._save_and_show(new_tests, selected_path, append=True)
        else:
            detail = ('; '.join(skipped))[:200] if skipped else ''
            sublime.error_message(t('import_failed', error=detail or t('no_test_files_in_dir')))

    # ---------- shared ----------

    def _save_and_show(self, tests, source_path, append=False):
        src_file = self.view.file_name()
        if not src_file:
            return

        if append:
            # Shared policy (input key, answers are only filled in, never
            # overwritten) - previously imports used a different key than
            # the Companion path, so the same input could end up twice.
            merged, conflicts = merge_tests(load_all_tests(src_file), tests)
            tests = merged
            if conflicts:
                sublime.status_message(t('answer_conflict_kept'))

        if save_tests(src_file, tests):
            sublime.status_message(t('imported_tests', count=len(tests),
                                     source=os.path.basename(source_path)))
            # 正常 Run 流程: 复用/创建 -run 视图并从磁盘重新加载样例
            self.view.run_command('cph_view_tester', {'action': 'make_opd'})
        else:
            sublime.error_message(t('import_save_failed'))


class CphImportTestsHereCommand(CphImportTestsCommand):
    """Import every .in/.out pair sitting next to the source file.

    Same as the folder import but without the path prompt: data packs from
    an OJ (and this package's own "Export tests as .in/.out files") land in
    the directory of the file being solved, so that is the folder to scan.
    """

    def run(self, edit):
        src_file = self.view.file_name()
        if not src_file:
            sublime.status_message(t('save_file_first'))
            return
        self._do_import_from_folder(os.path.dirname(src_file),
                                    self.view.window())


class CphPickImportFileCommand(sublime_plugin.TextCommand):
    """Pick ONE data file next to the source file and import it.

    A directory would be listed by a plain "every file here" scan, and
    opening one is what produced `[Errno 13] Permission denied`.
    """

    def is_visible(self, event=None, **kwargs):
        """Context menu: only offered where this package applies.

        A .sublime-menu item has no `context` key in Sublime Text, so the
        command decides (Default's own `open_context_url` does the same).
        """
        return context_menu_visible(self.view, event)

    def run(self, edit):
        window = self.view.window()
        if not window:
            return

        src_file = self.view.file_name()
        if not src_file:
            sublime.status_message(t('save_file_first'))
            return

        src_dir = os.path.dirname(src_file)

        candidates = []
        try:
            entries = sorted(os.listdir(src_dir))
        except Exception as e:
            sublime.error_message(t('read_failed', error=str(e)))
            return
        for f in entries:
            full = os.path.join(src_dir, f)
            # os.path.isfile() alone is not enough: a symlink to a
            # directory is a file for the OS but open() refuses it.
            if not _is_file_like(full):
                continue
            if (f.endswith('.in') or f.endswith('.out') or
                    f.endswith('.in.txt') or f.endswith('.out.txt') or
                    f.endswith('.ans') or f == 'in.txt' or
                    f == 'input.txt' or f == 'out.txt' or
                    f == 'output.txt' or f == 'sample.txt' or
                    (f.endswith('.txt') and not f.endswith('__tests'))):
                candidates.append([f, full])

        if not candidates:
            sublime.status_message(t('no_test_files_in_dir'))
            return

        def on_select(idx):
            if idx < 0 or idx >= len(candidates):
                return
            _, file_path = candidates[idx]
            self.view.run_command('cph_import_tests_file', {'file_path': file_path})

        window.show_quick_panel(candidates, on_select)


class CphImportTestsFileCommand(sublime_plugin.TextCommand):
    def run(self, edit, file_path=None):
        if not file_path:
            return

        src_file = self.view.file_name()
        if not src_file:
            sublime.status_message(t('save_file_first'))
            return

        if _is_dir(file_path):
            sublime.error_message(t('path_is_directory', path=file_path))
            return

        candidates = []
        base, ext = os.path.splitext(file_path)
        for new_ext in ['.out', '.ans']:
            p = base + new_ext
            if _is_file_like(p):
                candidates.append(p)
        if ext in ('.in', '.in.txt', '.txt') and 'in' in os.path.basename(file_path).lower():
            for new_name in ['out.txt', 'output.txt', 'stdout.txt', 'ans.txt']:
                p = os.path.join(os.path.dirname(file_path), new_name)
                if _is_file_like(p):
                    candidates.append(p)

        try:
            inp = _read_text(file_path)
            out = ''
            if candidates:
                out = _read_text(candidates[0]).strip()
            tests = [{'test': inp, 'correct_answers': [out] if out else []}]
            # Same merge policy as every other import path (dedup by input,
            # answers only filled in) instead of a bespoke (input, answer)
            # key that let a re-import create a duplicate test point.
            saved, total, conflicts = merge_into_file(src_file, tests)
            if saved:
                sublime.status_message(t('imported_tests', count=total,
                                         source=os.path.basename(file_path)))
                if conflicts:
                    sublime.status_message(t('answer_conflict_kept'))
                self.view.run_command('cph_view_tester', {'action': 'make_opd'})
            else:
                sublime.error_message(t('import_save_failed'))
        except Exception as e:
            sublime.error_message(t('import_failed', error=str(e)))
