"""
Algorithm Competition Assistant - 界面语言切换（中文 / English）
"""

import sublime
import sublime_plugin

from .core.cph_i18n import set_lang, get_lang, t, LANG_ZH, LANG_EN
from .core.cph_settings import settings_file


def _apply(language):
    """Switch the UI, remember the choice, confirm it in the new language.

    The confirmation goes through t() after the switch, so the user reads it in
    the language they just picked. It used to be a hardcoded lowercase English
    literal that showed up even in a Chinese interface, and the two commands
    each carried their own copy of the whole body.
    """
    set_lang(language)
    settings = sublime.load_settings(settings_file)
    settings.set('language', language)
    sublime.save_settings(settings_file)
    if language == LANG_ZH:
        msg = t('lang_switched_zh')
    else:
        msg = t('lang_switched_en')
    sublime.status_message('[Algorithm Competition Assistant] ' + msg)


class CphLanguageSetLanguageCommand(sublime_plugin.TextCommand):
    def run(self, edit, language='en'):
        _apply(language)

    def is_enabled(self, language='en'):
        return True


class CphLanguageToggleLanguageCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        _apply(LANG_EN if get_lang() == LANG_ZH else LANG_ZH)
