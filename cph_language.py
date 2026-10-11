"""
cph-by-chenkx - language switching command (Chinese / English)
"""

import sublime
import sublime_plugin

from .core.cph_i18n import set_lang, get_lang, LANG_ZH, LANG_EN
from .core.cph_settings import settings_file


class CphLanguageSetLanguageCommand(sublime_plugin.TextCommand):
    def run(self, edit, language='en'):
        set_lang(language)
        settings = sublime.load_settings(settings_file)
        settings.set('language', language)
        sublime.save_settings(settings_file)
        if language == LANG_ZH:
            sublime.status_message('algorithm competition assistant: switched to Chinese')
        else:
            sublime.status_message('algorithm competition assistant: switched to English')

    def is_enabled(self, language='en'):
        return True


class CphLanguageToggleLanguageCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        current = get_lang()
        new_lang = LANG_EN if current == LANG_ZH else LANG_ZH
        set_lang(new_lang)
        settings = sublime.load_settings(settings_file)
        settings.set('language', new_lang)
        sublime.save_settings(settings_file)
        if new_lang == LANG_ZH:
            sublime.status_message('algorithm competition assistant: switched to Chinese')
        else:
            sublime.status_message('algorithm competition assistant: switched to English')
