"""
Algorithm Competition Assistant - 评判系统 (类似 cph-ng)
"""

import re

VERDICT_NAME = {
    'unknown_error': 'UKE',
    'accepted': 'AC',
    'partially_correct': 'PC',
    'presentation_error': 'PE',
    'wrong_answer': 'WA',
    'time_limit_exceed': 'TLE',
    'memory_limit_exceed': 'MLE',
    'output_limit_exceed': 'OLE',
    'runtime_error': 'RE',
    'restricted_function': 'RF',
    'compilation_error': 'CE',
    'system_error': 'SE',
    'waiting': 'WT',
    'compiling': 'CP',
    'compiled': 'CPD',
    'judging': 'JG',
    'judged': 'JGD',
    'comparing': 'CMP',
    'skipped': 'SK',
    'rejected': 'RJ',
}
VERDICT_TYPE = {
    'running': 'RUNNING',
    'passed': 'PASSED',
    'failed': 'FAILED',
}

VERDICTS = {
    'unknown_error': {
        'name': VERDICT_NAME['unknown_error'],
        'color': '#0000ff',
        'type': VERDICT_TYPE['failed'],
    },
    'accepted': {
        'name': VERDICT_NAME['accepted'],
        'color': '#49cd32',
        'type': VERDICT_TYPE['passed'],
    },
    'partially_correct': {
        'name': VERDICT_NAME['partially_correct'],
        'color': '#ed9813',
        'type': VERDICT_TYPE['failed'],
    },
    'presentation_error': {
        'name': VERDICT_NAME['presentation_error'],
        'color': '#ff778e',
        'type': VERDICT_TYPE['failed'],
    },
    'wrong_answer': {
        'name': VERDICT_NAME['wrong_answer'],
        'color': '#d3140d',
        'type': VERDICT_TYPE['failed'],
    },
    'time_limit_exceed': {
        'name': VERDICT_NAME['time_limit_exceed'],
        'color': '#0c0066',
        'type': VERDICT_TYPE['failed'],
    },
    'memory_limit_exceed': {
        'name': VERDICT_NAME['memory_limit_exceed'],
        'color': '#5300a7',
        'type': VERDICT_TYPE['failed'],
    },
    'output_limit_exceed': {
        'name': VERDICT_NAME['output_limit_exceed'],
        'color': '#8300a7',
        'type': VERDICT_TYPE['failed'],
    },
    'runtime_error': {
        'name': VERDICT_NAME['runtime_error'],
        'color': '#1a26c8',
        'type': VERDICT_TYPE['failed'],
    },
    'restricted_function': {
        'name': VERDICT_NAME['restricted_function'],
        'color': '#008f81',
        'type': VERDICT_TYPE['failed'],
    },
    'compilation_error': {
        'name': VERDICT_NAME['compilation_error'],
        'color': '#8b7400',
        'type': VERDICT_TYPE['failed'],
    },
    'system_error': {
        'name': VERDICT_NAME['system_error'],
        'color': '#000000',
        'type': VERDICT_TYPE['failed'],
    },
    'waiting': {
        'name': VERDICT_NAME['waiting'],
        'color': '#4100d9',
        'type': VERDICT_TYPE['running'],
    },
    'compiling': {
        'name': VERDICT_NAME['compiling'],
        'color': '#5e19ff',
        'type': VERDICT_TYPE['running'],
    },
    'judging': {
        'name': VERDICT_NAME['judging'],
        'color': '#844fff',
        'type': VERDICT_TYPE['running'],
    },
    'skipped': {
        'name': VERDICT_NAME['skipped'],
        'color': '#9e9e9e',
        'type': VERDICT_TYPE['failed'],
    },
}


VERDICTS_BY_NAME = {}
for _v in VERDICTS.values():
    VERDICTS_BY_NAME[_v['name']] = _v


def get_verdict(name):
    return VERDICTS.get(name, VERDICTS['unknown_error'])


def get_verdict_by_name(name):
    """Return the verdict dict for a short name (e.g. 'WA'), or None."""
    if not name:
        return None
    return VERDICTS_BY_NAME.get(name)


# Ways a crashed program reports WHERE it died. Most specific first.
#
# The file match is lazy (`+?`) and the class also accepts ':' and '+', so
# Windows absolute paths ("D:/proj/c++/main.cpp:12:5") survive: a greedy
# class without ':' truncated them at the drive letter and the resulting
# jump link pointed at a path that does not exist.
_CRASH_PATTERNS = (
    # gcc/clang -fsanitize=address,undefined:  main.cpp:12:5: runtime error: ...
    # The plugin's own debug mode uses the trap form
    # (-fsanitize=undefined -fsanitize-trap=undefined), which prints NOTHING:
    # the check is a ud2 instruction, so the process dies on 0xC000001D and
    # this pattern never fires. That is the case core/cph_gdb_trace.py exists
    # for - it replays the crash under gdb and reads the frame instead.
    re.compile(r'([A-Za-z0-9_./\\:+-]+?\.(?:c|cc|cpp|cxx|h|hpp)):(\d+):\d+:\s*'
               r'(?:runtime error|AddressSanitizer|ERROR|SUMMARY)'),
    # libstdc++ / glibc assertions (-D_GLIBCXX_ASSERTIONS, C assert()):
    #   D:/.../bits/stl_vector.h:1263: ...: Assertion '__n < this->size()' failed.
    #   main.cpp:9: int main(): Assertion `x >= 0' failed.
    # These carry 文件:行 but no column, so the compiler-diagnostic pattern
    # below (which demands a column) never sees them. This is the locating
    # fallback on toolchains where the sanitizer runtime cannot link
    # (e.g. MSYS2 mingw gcc: "cannot find -lubsan").
    re.compile(r'([A-Za-z0-9_./\\:+-]+?\.(?:c|cc|cpp|cxx|h|hpp)):(\d+):'
               r'[^\n]*\bAssertion\b'),
    # Python traceback:  File "main.py", line 12
    re.compile(r'File "([^"]+)", line (\d+)'),
    # Java stack trace:  at Main.main(Main.java:12)
    re.compile(r'\(([A-Za-z0-9_./\\$-]+\.java):(\d+)\)'),
    # gdb / backtrace:  ... at main.cpp:12
    re.compile(r'\bat\s+([A-Za-z0-9_./\\-]+\.(?:c|cc|cpp|cxx|py|java)):(\d+)'),
    # any compiler-style diagnostic:  main.cpp:12:5
    re.compile(r'([A-Za-z0-9_./\\-]+\.(?:c|cc|cpp|cxx)):(\d+):\d+'),
)


def find_crash_location(text, source_file=None):
    """Best-effort (file, line) for where a crashed program died, or None.

    A plain segfault carries no line number, so this only reports what the
    program's own output gives us: a Python traceback, a Java stack trace,
    gcc/clang -fsanitize diagnostics or a gdb backtrace. A location matching
    the source file being tested wins over the first unrelated one.

    This parses TEXT. When there is no text to parse - the trap form of ubsan
    dies on ud2 with an empty stderr, and a bare segfault prints nothing -
    the caller replays the program under gdb instead
    (``core/cph_gdb_trace.py``, whose ``find_crash`` is the text-parsing twin
    of this function: same ``(file, line)`` contract, same ``source_file``
    preference). The two are deliberately separate modules - one is pure
    string work, the other spawns a debugger - and the callers in
    ``test_manager.py`` / ``core/cph_parallel.py`` chain them: try this, and
    if the result does not name the source file, replay.
    """
    if not text:
        return None
    wanted = None
    if source_file:
        wanted = source_file.replace('\\', '/').rsplit('/', 1)[-1]
    fallback = None
    for pattern in _CRASH_PATTERNS:
        for match in pattern.finditer(text):
            path, line = match.group(1), int(match.group(2))
            if wanted and path.replace('\\', '/').rsplit('/', 1)[-1] == wanted:
                return (path, line)
            if fallback is None:
                fallback = (path, line)
        if fallback is not None:
            return fallback
    return fallback


def tokenize(s):
    if not s:
        return []
    return s.split()


def numbers_equal(a, b, tolerance):
    """Compare two tokens with a numeric tolerance when both look numeric.

    tolerance is used both as an absolute floor and as a relative factor:
    |a - b| <= tolerance * max(1, |a|, |b|)
    """
    try:
        fa = float(a)
        fb = float(b)
    except (TypeError, ValueError):
        return None
    if fa != fa and fb != fb:  # both NaN
        return True
    scale = max(1.0, abs(fa), abs(fb))
    return abs(fa - fb) <= tolerance * scale


def outputs_equal(actual, expected, float_tolerance=0):
    """Compare program output with the expected answer.

    float_tolerance <= 0 (default): exact token comparison.
    float_tolerance > 0: numeric tokens are allowed to differ within
    the given relative/absolute tolerance (floating point problems).
    """
    if actual is None or expected is None:
        return False
    a_tokens = tokenize(actual)
    e_tokens = tokenize(expected)
    if len(a_tokens) != len(e_tokens):
        return False
    if float_tolerance and float_tolerance > 0:
        for a, e in zip(a_tokens, e_tokens):
            if a == e:
                continue
            verdict_numeric = numbers_equal(a, e, float_tolerance)
            if verdict_numeric is None or not verdict_numeric:
                return False
        return True
    return a_tokens == e_tokens


def get_verdict_by_code(rtcode, runtime, time_limit_ms, memory_limit_mb,
                        stderr, stdout, expected_output,
                        ignore_error=True, ole_size=None, regard_pe_as_ac=False,
                        memory_used_mb=None, float_tolerance=0):
    if rtcode is None:
        return get_verdict('unknown_error')

    # An exit code that names *how* the process was killed is more specific
    # than the generic crash rule, so it is tested first: 137/9 (SIGKILL) is
    # what an OOM killer or the OS reports, 124/142 is what timeout(1) and
    # SIGALRM produce. is_crash_exit_code() covers 128..192, so testing it
    # first made both branches below unreachable (dead code).
    if rtcode != 0:
        if rtcode == 137 or rtcode == 9:
            return get_verdict('memory_limit_exceed')
        if rtcode == 124 or rtcode == 142:
            return get_verdict('time_limit_exceed')
        # Anything else non-zero is a crash, including a signal (negative on
        # POSIX) or an NTSTATUS value: RE even when the runtime is also over
        # the limit, because dying on an access violation at 2.1s is an RE.
        return get_verdict('runtime_error')

    if time_limit_ms and runtime and runtime > time_limit_ms:
        # ...unless the output shows a crash: the watchdog kill and the crash
        # happened at (nearly) the same moment, and RE is the honest verdict.
        # stdout is checked too, because with separate stderr off the crash
        # text (an assert / sanitizer message) lands there - only looking at
        # stderr reported a plain TLE for a program that had aborted.
        if looks_like_crash(stderr) or looks_like_crash(stdout):
            return get_verdict('runtime_error')
        return get_verdict('time_limit_exceed')

    # Real MLE: only reachable once the peak memory was actually measured
    if memory_limit_mb and memory_used_mb and memory_used_mb > float(memory_limit_mb):
        return get_verdict('memory_limit_exceed')

    # A program that died on its own still exits 0 when the abort happens
    # inside a destructor, a signal handler, or a sanitizer that calls
    # _exit(0). The crash text is the only witness, so it is checked before
    # the output comparison, not after: otherwise a program that printed the
    # right answer first and crashed after the last write scored AC on a
    # sanitizer report. ignore_error=True can suppress stderr, never this.
    if stdout is not None and stdout is not expected_output and looks_like_crash(stdout):
        return get_verdict('runtime_error')

    if not expected_output or not expected_output.strip():
        # 没有设置正确答案时,不能判定为 AC,返回未评判
        return get_verdict('unknown_error')

    if not ignore_error and stderr and stderr.strip():
        return get_verdict('runtime_error')

    def fix(s):
        if not s:
            return ''
        return '\n'.join(line.rstrip() for line in s.rstrip().split('\n'))

    fixed_actual = fix(stdout)
    fixed_expected = fix(expected_output)

    if ole_size and fixed_actual and fixed_expected and len(fixed_actual) > len(fixed_expected) * ole_size:
        return get_verdict('output_limit_exceed')

    if float_tolerance and float_tolerance > 0:
        if not outputs_equal(stdout, expected_output, float_tolerance):
            return get_verdict('wrong_answer')
        return get_verdict('accepted')

    # Compare the token sequence, not a whitespace-stripped blob: joining
    # every split() made "1 23" match the answer "12 3", so a wrong answer
    # fell through to the presentation-error rule below and told the user
    # their numbers were right.
    if not outputs_equal(stdout, expected_output, 0):
        return get_verdict('wrong_answer')

    if fixed_actual != fixed_expected and not regard_pe_as_ac:
        return get_verdict('presentation_error')

    return get_verdict('accepted')


# ---------------------------------------------------------------------------
# Runtime error detection
#
# A crashed program does not always announce itself with a nice exit code:
# the watchdog may have killed it a moment later, or the process may exit 0
# after printing the failure to stderr. These two helpers let the judge say
# RE instead of a misleading TLE / WA.
# ---------------------------------------------------------------------------

#: Windows NTSTATUS exception codes (access violation, divide by zero,
#: stack overflow, ...) start here.
_WINDOWS_EXCEPTION_BASE = 0xC0000000
#: A shell reports "died from signal N" as 128 + N.
_POSIX_SIGNAL_BASE = 128
_POSIX_SIGNAL_MAX = 192

_CRASH_TEXT = (
    # Python
    re.compile(r'Traceback \(most recent call last\)'),
    re.compile(r'\b(?:IndexError|KeyError|ValueError|ZeroDivisionError|'
               r'RecursionError|MemoryError|NameError|TypeError|'
               r'AttributeError|OverflowError|ArithmeticError|AssertionError|'
               r'RuntimeError|StopIteration|UnboundLocalError)\b'),
    # C/C++ runtimes and tools
    re.compile(r'\bterminate called after throwing\b'),
    re.compile(r'\bstd::(?:bad_alloc|bad_cast|out_of_range|length_error|'
               r'invalid_argument|logic_error|runtime_error|domain_error|'
               r'range_error|overflow_error|underflow_error)\b'),
    re.compile(r'(?:Address|UndefinedBehavior|Thread|Leak)Sanitizer'),
    re.compile(r'\bruntime error:'),
    re.compile(r'Assertion .* failed'),
    re.compile(r'\b(?:Segmentation fault|Bus error|Floating point exception|'
               r'core dumped|stack smashing)\b'),
    re.compile(r'\bstack overflow\b', re.I),
    # Java / JVM
    re.compile(r'Exception in thread "'),
    re.compile(r'\bjava\.lang\.[A-Za-z]*(?:Exception|Error)\b'),
    re.compile(r'\b(?:StackOverflowError|OutOfMemoryError|NullPointerException)\b'),
)


def is_crash_exit_code(rtcode):
    """True when an exit code means 'the program died', not 'it finished'.

    POSIX signals are reported as negative codes by Python, Windows reports
    an NTSTATUS value such as 0xC0000005 (access violation), and a shell
    wrapper uses 128 + signal number.
    """
    if rtcode is None:
        return False
    try:
        code = int(rtcode)
    except (TypeError, ValueError):
        return False
    if code < 0:
        return True
    if code >= _WINDOWS_EXCEPTION_BASE:
        return True
    if _POSIX_SIGNAL_BASE < code <= _POSIX_SIGNAL_MAX:
        return True
    return False


def looks_like_crash(text):
    """True when an error stream carries a runtime-error signature.

    Deliberately narrow: plain `cerr` debug output must never turn a run
    into RE, only text that a crash handler / sanitizer / interpreter
    traceback actually produces.
    """
    if not text:
        return False
    for pattern in _CRASH_TEXT:
        if pattern.search(text):
            return True
    return False


def normalize_lines(s):
    """Split output into lines, stripping trailing whitespace per line
    and trailing newlines at the end (contest answer comparison)."""
    if not s:
        return []
    s = s.replace('\r\n', '\n').replace('\r', '\n')
    s = s.rstrip('\n')
    return [line.rstrip() for line in s.split('\n')]


#: SequenceMatcher is quadratic in the worst case and this runs on the UI
#: thread every time a detail view opens. Measured before the guards below:
#: 20k lines of 0/1 output with 20 changed lines took 56s to diff; 8k
#: identical lines with one changed line took 11.8s. Both froze Sublime.
#: Trimming removes the common shape (output that matches except somewhere
#: in the middle), and this cell budget bounds what is left at ~0.15s.
_DIFF_CELL_BUDGET = 1000000


def build_line_diff(expected, actual):
    """Line-by-line diff between expected and actual output.

    Trailing whitespace of each line is ignored; trailing empty lines
    are ignored as well.

    Returns (ops, total_lines):
      ops is None when both outputs match;
      otherwise a list of (kind, line_no, expected_line, actual_line):
        kind '!=': both sides have the line but contents differ
        kind '-' : line only exists in the expected output
        kind '+' : line only exists in the actual output
      total_lines is the number of lines of the longer side.
    """
    exp = normalize_lines(expected)
    act = normalize_lines(actual)
    total = max(len(exp), len(act))

    if exp == act:
        return None, total

    # Only the region between the first and the last difference can produce
    # a non-equal opcode, so SequenceMatcher is handed just that window.
    # Line numbers below are offset back by `head`.
    limit = min(len(exp), len(act))
    head = 0
    while head < limit and exp[head] == act[head]:
        head += 1
    tail = 0
    while (tail < limit - head
           and exp[len(exp) - 1 - tail] == act[len(act) - 1 - tail]):
        tail += 1
    we = exp[head:len(exp) - tail]
    wa = act[head:len(act) - tail]

    ops = []
    if len(we) * len(wa) > _DIFF_CELL_BUDGET:
        # Still too big to align without freezing the editor. Fall back to a
        # straight line-by-line scan of the window: it never invents a
        # difference (an identical line at the same position is skipped) and
        # it is O(n). autojunk=True would be fast as well, but it merges runs
        # of repeated lines into one giant replace block, so a single changed
        # line in 10k identical ones comes back as "10k lines differ".
        # A shift makes every following position differ, which is what a
        # positional diff looks like anywhere else too.
        shared = min(len(we), len(wa))
        for off in range(shared):
            if we[off] != wa[off]:
                ops.append(('!=', head + off + 1, we[off], wa[off]))
        for off in range(shared, len(we)):
            ops.append(('-', head + off + 1, we[off], ''))
        for off in range(shared, len(wa)):
            ops.append(('+', head + off + 1, '', wa[off]))
        return ops, total

    # Imported here: difflib costs ~29 ms of cold import and is only reached
    # when a mismatch is being explained in the detail view, not on a clean AC.
    import difflib
    sm = difflib.SequenceMatcher(a=we, b=wa, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if tag == 'replace':
            span = max(i2 - i1, j2 - j1)
            for off in range(span):
                e = we[i1 + off] if i1 + off < i2 else ''
                a = wa[j1 + off] if j1 + off < j2 else ''
                ops.append(('!=', head + i1 + off + 1, e, a))
        elif tag == 'delete':
            for k in range(i1, i2):
                ops.append(('-', head + k + 1, we[k], ''))
        elif tag == 'insert':
            for k in range(j1, j2):
                ops.append(('+', head + k + 1, '', wa[k]))
    return ops, total
