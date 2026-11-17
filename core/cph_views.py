"""View lifecycle helpers: which scratch views belong to a source file, and
how to close them cleanly when the source closes or Sublime exits.

Root-level plugin modules may not import each other (Package Control rule), so
the shared logic lives here in core/, imported by test_manager.py (the close
listener) and plugin_init.py (unload). Keeping it in one place matters: the
run panel, the per-test detail tabs and the test-edit tabs are three separate
view kinds created by two different modules, and every close path has to agree
on what "belongs to this source" means or a stale tab is left behind.

Identity is by *setting*, not by view name: basenames collide (two main.cpp in
different folders) and names are translated. Every view created by the panel
carries either cph_run_view + cph_run_source, cph_detail_source, or
cph_edit_view + cph_edit_source (the run view's id, from test_edit.py).
"""

import os

import sublime

RUN_FLAG = 'cph_run_view'
RUN_SOURCE = 'cph_run_source'
DETAIL_SOURCE = 'cph_detail_source'
EDIT_FLAG = 'cph_edit_view'
EDIT_SOURCE = 'cph_edit_source'


def _norm(path):
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except Exception:
        return path


def tag_run_view(view, source_file):
    """Mark a view as this source's run panel, carrying the source path."""
    try:
        view.settings().set(RUN_FLAG, True)
        view.settings().set(RUN_SOURCE, _norm(source_file))
    except Exception:
        pass


def tag_detail_view(view, source_file, index):
    """Mark a view as a detail tab of this source."""
    try:
        view.settings().set(DETAIL_SOURCE, _norm(source_file))
    except Exception:
        pass


def tag_edit_view(view, run_view_id):
    """Mark a view as a test-edit tab of a run panel (id kept for pairing)."""
    try:
        view.settings().set(EDIT_FLAG, True)
        view.settings().set(EDIT_SOURCE, run_view_id)
    except Exception:
        pass


def views_owned_by(window, source_file, source_view_id=None):
    """All of one source's scratch views in `window` (run/detail/edit).

    `source_view_id` is the *code* view's id. The edit tabs store the run
    view's id instead, so they are matched through the run view found here;
    falling back to the code id keeps older tabs (and direct opens) working.

    A legacy run panel that has cph_run_view but no cph_run_source is matched
    by its '<basename> -run' name, which is the only identity older builds
    left behind.
    """
    owned = []
    if window is None or not source_file:
        return owned
    want = _norm(source_file)
    run_ids = set()
    try:
        views = list(window.views())
    except Exception:
        return owned
    for view in views:
        try:
            s = view.settings()
            if s.get(RUN_FLAG):
                tagged = s.get(RUN_SOURCE)
                if tagged:
                    if _norm(tagged) == want:
                        owned.append(view)
                        run_ids.add(view.id())
                elif (view.name() or '') == \
                        os.path.basename(source_file) + ' -run':
                    # Pre-tag panel from an older session.
                    owned.append(view)
                    run_ids.add(view.id())
            elif s.get(DETAIL_SOURCE):
                if _norm(s.get(DETAIL_SOURCE)) == want:
                    owned.append(view)
        except Exception:
            continue
    for view in views:
        try:
            if view in owned:
                continue
            s = view.settings()
            if s.get(EDIT_FLAG):
                owner = s.get(EDIT_SOURCE)
                if owner in run_ids or (source_view_id is not None
                                        and owner == source_view_id):
                    owned.append(view)
        except Exception:
            continue
    return owned


def plugin_views(window):
    """Every scratch view this plugin owns in `window`, of any kind."""
    out = []
    if window is None:
        return out
    try:
        views = list(window.views())
    except Exception:
        return out
    for view in views:
        try:
            s = view.settings()
            if (s.get(RUN_FLAG) or s.get(DETAIL_SOURCE) or s.get(EDIT_FLAG)
                    or (view.name() or '').endswith(' -run')):
                out.append(view)
        except Exception:
            continue
    return out


def close_views(views):
    """Close scratch views, each guarded: one bad close must not stop the rest.

    Callers that close from on_pre_close defer the call through set_timeout
    (ST4215 SIGSEGVs when a view is closed inside the host's teardown frame);
    this helper does not defer, because unload has no event loop left to run a
    timeout reliably.
    """
    for view in views or ():
        try:
            view.close()
        except Exception:
            pass


def restore_layout(window):
    """Undo the two-column panel layout and re-show the file sidebar.

    Only called for a window that actually held one of our panels, so a
    user's own split layout in an unrelated window is never touched.
    """
    if window is None:
        return
    try:
        window.set_sidebar_visible(True)
    except Exception:
        pass
    try:
        layout = window.get_layout() or {}
        cols = layout.get('cols') or []
        # A single-column layout already has 2 boundaries; our panel layout
        # has 3 ([0, ruler, 1]). Only collapse the latter, and leave any
        # user-made split in a window we did not touch alone.
        if len(cols) <= 2:
            return
        window.set_layout({'cols': [0, 1], 'rows': [0, 1],
                           'cells': [[0, 0, 1, 1]]})
    except Exception:
        pass


def cleanup_on_exit():
    """Close every panel/detail/edit tab and restore each window's layout.

    Sublime calls plugin_unloaded both on quit and on upgrade / .py edit. In
    either case a scratch panel left behind points at a dead process and an
    old build, so closing them is correct; restoring the layout only for
    windows that held a panel leaves untouched windows alone.
    """
    try:
        windows = list(sublime.windows())
    except Exception:
        return
    for window in windows:
        views = plugin_views(window)
        if not views:
            continue
        close_views(views)
        restore_layout(window)
