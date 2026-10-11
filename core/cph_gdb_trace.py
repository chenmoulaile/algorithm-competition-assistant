"""Algorithm Competition Assistant - 用 gdb 把"静默的未定义行为"变成一行可点击的源码位置

问题
----
``std::vector`` 越界读是未定义行为，**不是崩溃**。``-O2`` 下它读到堆垃圾、打印一个
随机数、``exit 0`` —— 插件只能判 WA，整条流程没有任何一屏会提示越界了。加
``-D_GLIBCXX_ASSERTIONS`` 让同一个程序中止，但本机（MSYS2 mingw gcc 15.2）实测下来
只是把"看不见"换成"看得见但看不懂"，另外还引入一个新误判：

1. **定位不到用户代码。** 断言消息里的路径是 libstdc++ 的
   ``bits/stl_vector.h:1263``（``operator[]`` 自己），不是用户越界的那一行。
2. **断言文本在进程退出前一个字节都拿不到**（实测：断言第 0 秒触发，stderr 的**第一个
   字节**在 4.77s 出现，而进程也在 4.77s 才退出，rc=3）。abort 路径要经过 Windows 错误
   报告（WER），那几秒里什么都不写。时限 watchdog 在 2s 秒杀，``looks_like_crash()``
   看到的是空字符串，于是判 TLE。**这就是"切到 debug 就变成 TLE"的机制。**
3. 显式 ``abort()`` 和 ``__builtin_trap()`` 更极端：stderr **一个字节都没有**（实测
   exit_at 4.16s / 4.27s，final=0B），退出码分别是 3 和 3221225501（0xC0000003）。

方案
----
``gdb`` 本机可用（``D:\\MSYS2\\mingw64\\bin\\gdb.exe``）且带完整符号，把它套在程序外面跑
一次，断在 ``abort``，取一次回溯。实测四件事，全部是本机跑出来的：

- **快一个数量级**：同一个越界程序裸跑 4.66s，套上 gdb 只要 0.35s（p2_oob / p2_seg /
  g_oob 三次分别 0.35 / 0.30 / 0.33s）。原因很直接：gdb 自己的调试器接住了 abort 帧，
  Windows 错误报告根本没被触发，那四秒的 WER 根本不存在。
- **gdb 自己的退出码永远是 0**（实测正常退出 / 段错误 / abort 三种都是 0，而同样三个
  程序裸跑分别是 0 / 3221225477 / 3）。所以判定只能来自日志文本，**绝不看退出码**；
  代码里也就不用 ``quit 3`` 了 —— 那样会让正常程序也返回 3，全量误判 RE。
- **段错误不命中 abort 断点**（``break abort`` 下 ``hit=False``，gdb 停在
  ``Thread 1 received signal SIGSEGV``），但回溯照样给到了用户那帧
  （``#0 main () at t_seg.cpp:3``）。所以"命中"的判定同时看断点命中和收到信号两种。
- 正常程序在 gdb 下 stdout 只有自己的输出（``5\\r\\n``），比较判定不会被污染。

因此这里的 gdb 是**按需重放**：只有判出 RE 却没有用户帧、或者判出 TLE 的可疑中止时才
跑一次，输出不参与答案比较，结论以日志解析为准。找不到用户帧就退化成原有的 libstdc++
断言位置，绝不凭空编造行号。

为什么 sanitizer 之外还要 gdb
-----------------------------
debug 构建模式（``core/cph_build_mode.py``）用的 ubsan 形态是
``-fsanitize=undefined -fsanitize-trap=undefined``：它把每条 UBSan 检查换成一条 ``ud2``
指令，**链接期不需要 libubsan**，所以在本机（``libasan*`` / ``libubsan*`` 都不存在，
g++ 报 ``cannot find -lubsan/-lasan``）也能编过并真的抓到有符号溢出、栈数组越界、除零。
代价是它**没有诊断文本**——命中即 ``ud2``，进程以 ``0xC000001D``（SIGILL）退出，stderr
一个字节都没有，所以 ``core/cph_verdict.py`` 只能判出 RE，说不出在哪一行。gdb 回放就是
补这一步：把 SIGILL 和 SIGSEGV 一视同仁地接住，从回溯里读回用户帧的行号。

AddressSanitizer（堆越界、use-after-free，带 ``文件:行:列``）在本机链接不上，只能由用户
在 ``compile_cmd`` 里 opt-in；``-D_GLIBCXX_ASSERTIONS`` 覆盖 ``std::vector`` 越界，
它的断言文本自带 libstdc++ 的 ``bits/stl_vector.h:1263``（不是用户那行），
``core/cph_verdict.py`` 的 ``_CRASH_PATTERNS`` 第二条负责把它读出来，gdb 再把它换成
用户帧。三条链各管一段，互不替代。

回溯解析
--------
``#3  0x00007ff674c2156f in main () at D:\\...\\std.cpp:20`` —— 优先取路径文件名匹配被测
源文件的那一帧；没有匹配帧就取第一个带 ``at file:line`` 且不在已知系统目录
（``msys2`` / ``mingw`` / ``msvcrt`` / ``windows/system32`` / ``/usr/`` / ``cygwin``）
里的帧。都取不到返回 None。
"""

import os
import re
import subprocess

#: gdb 的回溯帧：``#3  0xADDR in FUNC () at /path/file.cpp:20``。
#: 只认带 ``at file:line`` 的帧 —— 少了位置的帧（``#1 in ?? () from x.dll``）
#: 只能给出行号 0，比不报还糟。
_FRAME = re.compile(r'^#(\d+)\s+0x[0-9a-fA-F]+\s+in\s+(.*?)\s+at\s+(.+?):(\d+)\s*$')

#: 断点落点。-D_GLIBCXX_ASSERTIONS 的终点就是 abort；显式 abort() 同理。**只断 abort
#: 一个**：试过加 'raise'，这个 gdb 报 'Function "raise" not defined.' 往日志灌噪声，
#: 什么也没多断到；段错误实测根本不走 abort（hit=False），由信号那一路接住。
#: 不含 _exit / exit：正常 return 不经过它们。
_BREAKPOINTS = ('abort',)

#: 这些目录里的帧不是用户的代码，不能拿来报位置。注意 mingw 必须在列表里：CRT 的
#: ``__tmainCRTStartup`` 帧带的是 ``D:/W/B/src/mingw-w64/...`` 这样的构建路径。
_SYSTEM_DIRS = ('msys2', 'mingw', 'msvcrt', 'windows/system32', '/usr/',
                'cygwin', 'git/mingw')

#: gdb 批处理里标记"回溯已打印"的分隔符。解析时按它切，避免把 gdb 的启动噪声
#: 当成回溯内容。
_SENTINEL = 'CPH_FRAME_BEGIN'
_SENTINEL_END = 'CPH_FRAME_END'

#: 命中断点。
_HIT_BREAKPOINT = re.compile(r'Thread\s+\S+\s+hit\s+Breakpoint')
#: 收到致命信号。段错误走这一条（实测不会命中 abort 断点）。
_HIT_SIGNAL = re.compile(r'Thread\s+\S+\s+received signal\s+(SIG[A-Z]+)')


def _hidden():
    """STARTUPINFO that keeps a console window from flashing, or None."""
    if os.name != 'nt':
        return None
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return startupinfo
    except Exception:
        return None

#: 顺序试这些地方找 gdb。第一个环境变量是给"gdb 不在 PATH 上"的机器留的手动出口。
_GDB_CANDIDATES = ('gdb', 'gdb.exe')
#: mingw / msys2 常见的安装位置，PATH 里没有时兜底。
_GDB_FALLBACK_DIRS = (
    r'D:\MSYS2\mingw64\bin',
    r'C:\msys64\mingw64\bin',
    r'C:\msys64\mingw32\bin',
    '/usr/bin',
)


def _norm(path):
    return path.replace('\\', '/').lower()


def parse_frames(text, source_file=None):
    """Backtrace text -> the user's own frame as (file, line), or None.

    Prefers a frame whose file is the source under test; otherwise the first
    frame outside the toolchain directories. Frames with no ``file:line`` are
    skipped: they can only produce a line number of 0, which is worse than
    reporting nothing.
    """
    if not text:
        return None
    wanted = None
    if source_file:
        wanted = _norm(source_file).rsplit('/', 1)[-1]
    fallback = None
    for raw in text.splitlines():
        m = _FRAME.match(raw.strip())
        if not m:
            continue
        path, lineno = m.group(3), int(m.group(4))
        low = _norm(path)
        if wanted and low.rsplit('/', 1)[-1] == wanted:
            return (path, lineno)
        if fallback is None and not any(d in low for d in _SYSTEM_DIRS):
            fallback = (path, lineno)
    return fallback


def _quote(path):
    """The program argument for gdb's ``--args``, verbatim.

    刻意**不加**引号。子进程传 argv 给 gdb 时边界由 CreateProcess 保证，gdb 把
    ``--args`` 之后的每个 argv 元素原样当程序名；而一旦自己加引号，gdb 会把引号
    当成路径的一部分，于是报 ``No executable specified, use `target exec'``。
    实测（用户真实路径，带空格且含中文）：

        --args "D:\\Code_Source\\...\\day7\\A\\std.exe"  -> 失败，中文还被按
                                                          ANSI 代码页解码成乱码
        --args D:\\Code_Source\\...\\day7\\A\\std.exe      -> 成功，0.87s 命中断点

    之前写成"有空格就加引号"，是因为测试夹具全在 %TEMP% 下、没有空格路径，把这个
    bug 完整地藏住了。
    """
    return path


def names_source(location, source_file):
    """True when `location` already points at the source under test.

    Decides whether a replay is worth running at all: an abort whose own
    message named a libstdc++ header (``stl_vector.h:1263``) is real but not
    actionable, and the frame the user has to fix is their own.
    """
    if not location or not source_file:
        return False
    try:
        got = location[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
        want = source_file.replace('\\', '/').rsplit('/', 1)[-1].lower()
    except Exception:
        return False
    return got == want


def find_gdb():
    """Path to a usable gdb, or None.

    Never raises and never prints: a missing debugger must degrade the RE
    report, not break the run.
    """
    override = os.environ.get('CPH_GDB')
    if override and os.path.isfile(override):
        return override
    candidates = []
    for name in _GDB_CANDIDATES:
        found = _which(name)
        if found:
            candidates.append(found)
    # g++ often sits next to gdb in a toolchain dir that is not on PATH.
    for compiler in ('g++', 'gcc', 'g++-15', 'g++-14'):
        found = _which(compiler)
        if not found:
            continue
        folder = os.path.dirname(found)
        for name in _GDB_CANDIDATES:
            guess = os.path.join(folder, name)
            if os.path.isfile(guess) and guess not in candidates:
                candidates.append(guess)
    for folder in _GDB_FALLBACK_DIRS:
        for name in _GDB_CANDIDATES:
            guess = os.path.join(folder, name)
            if os.path.isfile(guess) and guess not in candidates:
                candidates.append(guess)
    for guess in candidates:
        if os.path.isfile(guess):
            return guess
    return None


def _which(name):
    try:
        for folder in os.environ.get('PATH', '').split(os.pathsep):
            if not folder:
                continue
            guess = os.path.join(folder.strip('"'), name)
            if os.path.isfile(guess):
                return guess
    except Exception:
        pass
    return None


def build_gdb_cmd(gdb, program, log_path):
    """The command that runs `program` under gdb, logging to `log_path`.

    Returns None when there is no program to run.

    刻意不加 ``quit <code>``: 实测 gdb 的退出码与被测程序无关（正常退出 / 段错误 /
    abort 三种都是 0），任何靠退出码的判定都会把正常程序一起误判成 RE。结论一律
    从日志文本里读。

    ``set logging enabled on/off/on`` 那个 off/on 是必须的：少了它，gdb 的
    "Currently logging" 通知会漏到被测程序的 stdout 上，污染答案比较。

    ``kill`` 是为了在信号停下后收掉被调试进程，否则 gdb 一退出它就变成孤儿继续跑。
    """
    if not program:
        return None
    ex = [
        'set pagination off',
        'set confirm off',
        'set print thread-events off',
        'set backtrace past-main on',
        'set logging file %s' % log_path,
        'set logging redirect on',
        'set logging enabled on',
        'set logging enabled off',
        'set logging enabled on',
    ]
    for brk in _BREAKPOINTS:
        ex.append('break %s' % brk)
    ex.append('echo \\n%s\\n\\n' % _SENTINEL)
    ex.append('run')
    # 回溯必须打在两个哨兵之间。顺序写错是静默的：split_log() 会切出一段空区间，
    # 所有查询都返回 None，而日志里明明有帧。
    ex.append('bt 30')
    ex.append('info locals')
    ex.append('echo \\n%s\\n' % _SENTINEL_END)
    ex.append('kill')
    ex.append('quit')
    parts = [gdb, '-batch', '-nx']
    for e in ex:
        parts += ['-ex', e]
    parts += ['--args', _quote(program)]
    return parts


def split_log(text):
    """(backtrace_text, crashed) from a gdb log file.

    ``crashed`` is True when the program stopped on the abort breakpoint *or*
    received a fatal signal. The backtrace region only exists then: after a
    normal exit gdb prints nothing between the sentinels, and asserting on the
    mere presence of 'abort' would fire on a resolved symbol name.
    """
    if not text:
        return '', False
    start = text.find(_SENTINEL)
    if start < 0:
        return '', False
    end = text.find(_SENTINEL_END, start + 1)
    body = text[start + len(_SENTINEL):end] if end > 0 else text[start:]
    crashed = bool(_HIT_BREAKPOINT.search(body) or _HIT_SIGNAL.search(body))
    return body, crashed


def find_crash(text, source_file=None):
    """One-call helper: gdb log text -> (file, line) or None.

    The gdb-log twin of ``core/cph_verdict.py``'s ``find_crash_location``:
    same ``(file, line)`` contract and the same "prefer the frame that names
    ``source_file``" rule, but it reads a gdb log rather than the program's own
    stderr. Callers try ``find_crash_location`` first and fall back to a replay
    here, so this one may return None freely.

    Returns None when the program did not crash under gdb, or when the
    backtrace has no user frame. An unreachable gdb therefore degrades to the
    existing libstdc++ assertion location instead of reporting nothing.
    """
    body, crashed = split_log(text)
    if not crashed:
        return None
    return parse_frames(body, source_file)


def _kill_tree(proc):
    """gdb 被超时打断时，它拉起来的被测程序不能留在后台跑。"""
    if proc is None or proc.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.Popen(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             startupinfo=_hidden()).wait(timeout=3)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def replay(gdb, program, log_path, stdin_bytes=None, timeout=5.0, cwd=None):
    """Run `program` under gdb once and return (stdout_text, log_text).

    ``stdout_text`` is the program's own output with gdb's chatter excluded
    (the logging redirect does that) - callers must NOT feed it into the
    answer comparison, it is only there for diagnostics.

    ``timeout`` bounds the *whole* replay. It matters most for the case that
    prompted this module: an aborting program is dead in milliseconds but
    takes ~4.5s to be reaped on Windows (WER), so without a bound the TLE
    path would hang for that long before it can even look at the log.

    Returns ('', '') when gdb is unusable, so a missing debugger degrades to
    the previous behaviour instead of failing the run.

    gdb 自己的抱怨会原样留在 ``log_path`` 里（``No executable specified`` 之类）。
    这不是给用户看的噪音，而是唯一能证明"插件宿主里到底加载了哪份代码"的现场
    证据：日志文件名带 plugin_host 的 PID，配合进程 StartTime 就能立刻区分
    "代码没改对"和"进程还在跑旧字节码"。面板只显示 TLE 和空崩溃行时，唯一能
    指路的就是这份文件。
    """
    cmd = build_gdb_cmd(gdb, program, log_path)
    if not cmd:
        return '', ''
    try:
        if os.path.exists(log_path):
            os.remove(log_path)
    except Exception:
        pass
    payload = stdin_bytes if isinstance(stdin_bytes, bytes) else b''
    if isinstance(stdin_bytes, str):
        payload = stdin_bytes.encode('utf-8', 'replace')
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=cwd, startupinfo=_hidden())
    except Exception:
        return '', ''
    try:
        out, _err = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A program that really does run forever lands here: no crash in the
        # log, so the caller keeps its TLE.
        _kill_tree(proc)
        try:
            out, _err = proc.communicate(timeout=2)
        except Exception:
            out = b''
    except Exception:
        _kill_tree(proc)
        return '', ''
    finally:
        _kill_tree(proc)
    log_text = ''
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            log_text = fh.read()
    except Exception:
        log_text = ''
    try:
        out_text = out.decode('utf-8', 'replace') if out else ''
    except Exception:
        out_text = ''
    return out_text, log_text