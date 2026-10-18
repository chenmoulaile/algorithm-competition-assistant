"""Algorithm Competition Assistant plugin initialization entrypoint."""
import os

import sublime

# Kept in sync with messages.json by tests/check_messages.py.
VERSION = '2.6.0'


def plugin_loaded():
    """Called when the package loads."""
    try:
        # Say which build this host actually loaded. Sublime does not hot
        # reload the package's .py files, so a run can easily be judged by
        # code from an earlier install - and the only symptom is a verdict
        # that "does not match the source on disk". Printing the version at
        # startup (visible in the console with View > Show Console) plus the
        # plugin host's StartTime answers that question immediately.
        print('[Algorithm Competition Assistant] %s loaded (pid %s)' % (VERSION, os.getpid()))
        # Defer language and settings initialization
        sublime.set_timeout(_init, 100)
    except Exception as e:
        print('[Algorithm Competition Assistant] Error in plugin_loaded: %s' % str(e))


def plugin_unloaded():
    print('[Algorithm Competition Assistant] plugin unloaded')
    try:
        # Drop cached HTML/CSS so an updated package is picked up, and stop
        # any listener so the port is released.
        from .core.cph_resources import clear_cache
        clear_cache()
    except Exception:
        pass


def _init():
    try:
        # Record which build this host loaded so the doctor can name it: root
        # level modules may not import each other under Package Control's
        # rules, so VERSION reaches them through core.cph_state.
        from .core.cph_state import set_plugin_version
        set_plugin_version(VERSION)
    except Exception:
        pass

    try:
        from .core.cph_resources import clear_cache
        clear_cache()
    except Exception:
        pass

    try:
        from .core.cph_i18n import set_lang, LANG_ZH, LANG_EN
        # Load language from platform settings file
        settings_file = 'Algorithm Competition Assistant.sublime-settings'
        settings = sublime.load_settings(settings_file)
        lang = settings.get('language', 'zh')
        if lang in (LANG_ZH, LANG_EN):
            set_lang(lang)

        # Apply a language change made in the settings file without a restart
        def _on_settings_change():
            try:
                new_lang = settings.get('language', 'zh')
                if new_lang in (LANG_ZH, LANG_EN):
                    set_lang(new_lang)
            except Exception:
                pass

        settings.add_on_change('cph_language', _on_settings_change)
        print('[Algorithm Competition Assistant] plugin loaded, language: %s' % lang)
    except Exception as e:
        print('[Algorithm Competition Assistant] Error setting up language: %s' % str(e))

    try:
        from .core.cph_settings import try_load_settings
        try_load_settings()
    except Exception as e:
        print('[Algorithm Competition Assistant] Error loading settings: %s' % str(e))

    try:
        # Say which install is actually running. Package Control has emptied
        # the folder install under a running user before, and the only symptom
        # was "my changes do nothing" - no error, because nothing was wrong
        # with the code that was loaded, it was just not their code.
        from .core.cph_install import describe_installs
        from .core.cph_settings import package_name
        from .core.cph_i18n import t
        lines = describe_installs(sublime.packages_path(), package_name,
                                  os.path.dirname(os.path.abspath(__file__)),
                                  VERSION)
        for line in lines:
            print('[Algorithm Competition Assistant] ' + line)
        if lines:
            sublime.status_message('%s: %s' % (package_name, t('install_check_warn')))
    except Exception as e:
        print('[Algorithm Competition Assistant] install check failed: %s' % str(e))
