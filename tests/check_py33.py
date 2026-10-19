"""Guard: the plugin host is Python 3.3 (Sublime Text ships it by default).

Fails when the codebase starts using syntax or stdlib APIs that only exist
in newer Pythons. This is what allowed `Popen(encoding=...)` (3.6+) to ship
and break every single run - see v1.4.1 in RELEASE_NOTES_CN.md.

    python tests/check_py33.py
"""

import ast
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# attribute -> minimum Python version
UNSUPPORTED_ATTRS = {
    'subprocess.run': '3.5',
    'math.isclose': '3.5',
    'math.gcd': '3.5',
    'os.scandir': '3.5',
    'os.path.commonpath': '3.5',
    'shutil.disk_usage': '3.3 ok',
    'str.removeprefix': '3.9',
    'str.removesuffix': '3.9',
    'datetime.fromisoformat': '3.7',
    'functools.cached_property': '3.8',
    'importlib.resources': '3.7',
    'time.monotonic_ns': '3.7',
    'secrets.token_hex': '3.6',
    'typing.NamedTuple': '3.6',
}

# keyword arguments that Popen/run did not accept before 3.6/3.7
UNSUPPORTED_CALL_KWARGS = {
    'Popen': {'encoding', 'errors', 'text', 'capture_output', 'check'},
    'run': {'encoding', 'errors', 'text', 'capture_output'},
}

SKIP_DIRS = {'.git', '__pycache__', '.workbuddy', 'tests', 'promo'}


def iter_py_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith('.py'):
                yield os.path.join(base, f)


def rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, '/')


def main():
    problems = []
    files = 0
    for path in iter_py_files():
        files += 1
        try:
            with open(path, 'r', encoding='utf-8') as f:
                src = f.read()
        except Exception as e:
            problems.append('%s: unreadable (%s)' % (rel(path), e))
            continue
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as e:
            problems.append('%s:%s: syntax error (%s)' % (rel(path), e.lineno, e.msg))
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                problems.append('%s:%d uses an f-string (3.6+)'
                                % (rel(path), node.lineno))
            if isinstance(node, ast.NamedExpr):
                problems.append('%s:%d uses the walrus operator (3.8+)'
                                % (rel(path), node.lineno))
            if isinstance(node, ast.AnnAssign):
                problems.append('%s:%d uses a variable annotation (3.6+)'
                                % (rel(path), node.lineno))
            if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncWith,
                                 ast.AsyncFor)):
                problems.append('%s:%d uses async/await (3.5+)'
                                % (rel(path), node.lineno))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if getattr(node.args, 'posonlyargs', None):
                    problems.append('%s:%d uses positional-only parameters (3.8+)'
                                    % (rel(path), node.lineno))
            if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
                problems.append('%s:%d uses dict unpacking {**x} (3.5+)'
                                % (rel(path), node.lineno))
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    full = '%s.%s' % (getattr(fn.value, 'id', ''), fn.attr)
                    version = UNSUPPORTED_ATTRS.get(full)
                    if version and not version.startswith('3.3'):
                        problems.append('%s:%d uses %s (%s+)'
                                        % (rel(path), node.lineno, full, version))
                    name = fn.attr
                elif isinstance(fn, ast.Name):
                    name = fn.id
                else:
                    name = None
                bad = UNSUPPORTED_CALL_KWARGS.get(name)
                if bad:
                    for kw in node.keywords:
                        if kw.arg in bad:
                            problems.append('%s:%d %s(%s=) needs Python 3.6/3.7+'
                                            % (rel(path), node.lineno, name, kw.arg))

        # text-mode reads via open() are fine; forbidden are the subprocess ones
        for m in re.finditer(r'universal_newlines\s*=\s*True', src):
            problems.append('%s: universal_newlines uses the locale encoding '
                            '(decode/encode UTF-8 explicitly instead)'
                            % rel(path))

    print('scanned %d python files' % files)
    if problems:
        print('%d Python 3.3 problem(s):' % len(problems))
        for p in problems:
            print('  - %s' % p)
        return 1
    print('Python 3.3 compatibility: OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
