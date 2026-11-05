# Algorithm Competition Assistant

Sublime Text 4（build 4095 及以上）的算法竞赛本地测评插件。在源码文件里按 `Ctrl+Alt+B`，
它按你配置的编译命令和运行命令跑测试点，给出判定、用时和内存；点 `详情` 能看到输入、
期望输出、实际输出、标准错误输出，以及逐行差异。

这个插件是 [FastOlympicCoding](https://github.com/Jatana/FastOlympicCoding) 的 fork。
运行面板、测试点编辑、对拍和 Competitive Companion 监听的骨架来自原项目，快捷键也沿用
原来的布局。判题结果的显示方式参考了 VSCode 的
[cph-ng](https://github.com/langningchen/cph-ng)，浏览器导入的思路参考了
[FastOlympicCodingHook](https://github.com/DrSchwad/FastOlympicCodingHook)。

改动的重点在本地判题：内存和超时按真实进程测量，运行错误尽量定位到源码行，编译有缓存，
另外补了文件输入输出、抓题、练习统计这些本地调试会用到的功能。它不解题，也不联网判题。

英文简介见 [README.md](README.md)，逐版本改动见 [RELEASE_NOTES_CN.md](RELEASE_NOTES_CN.md)。

## 安装

### 文件夹安装（想改代码就用这个）

1. 克隆或下载仓库；
2. 把整个文件夹放进 Sublime 的 `Packages` 目录，目录名随意；
3. 重启 Sublime。

Sublime 不热重载插件，改了 `.py` 要重启才生效。

### Package Control

`Ctrl+Shift+P` → `Package Control: Install Package` → 搜
`Algorithm Competition Assistant`。装完即用，升级走 `Package Control: Upgrade Package`。

Package Control 装的是 `Installed Packages/…sublime-package`，`Packages/` 下没有对应的
文件夹。要改配置，用 `Preferences → Package Settings → Algorithm Competition Assistant →
Settings`：左边是只读的默认值，右边是可编辑的
`Packages/User/Algorithm Competition Assistant.sublime-settings`，保存后升级不丢。

两种方式不要同时用。`Packages/` 下同时有同名文件夹和 zip 时，Package Control 升级 zip
会把那个文件夹清空，现象是"改了代码、重启、什么都没变"。启动时会在控制台和状态栏提示
检测到两份安装，`Ctrl+Alt+D` 会写明当前加载的是文件夹还是 zip。

## 上手

1. 打开源码文件。默认配置覆盖 C++、C、Python、Java、Rust、Go、Pascal、JavaScript，
   编译器要在 `PATH` 上。
2. `Ctrl+Alt+B` 运行。右侧面板每个测试点一张卡片，卡片上是 verdict、用时、内存。
3. 点 `详情` 看完整内容。点 `编辑` 打开输入和标准答案两个标签页，在答案页保存后会
   自动重判。
4. 命令面板搜 `Algorithm` 能看到全部命令，`View → Algorithm Competition Assistant`
   里有同样的一套。

默认 C++ 编译命令是 `g++ … -std=c++17 -O2`。`-O2` 不要省：没优化时，一个正确的程序
也可能跑到超时。

## 判题

### 判定标记

| 标记 | 含义 |
| --- | --- |
| `AC` | 通过 |
| `WA` | 输出与答案不一致 |
| `TLE` | 超过时限，进程已被终止 |
| `MLE` | 峰值内存超过上限 |
| `RE` | 非零退出、崩溃或触发断言 |
| `PE` | 内容一致，空白或换行不同 |
| `CE` | 编译失败 |
| `SK` | 被手动停止 |
| `UKE` | 没有得出结论，例如程序没启动起来 |

运行结束后卡片下面有 `accept` / `decline`：前者把当前输出记成正确答案，后者记成错误
答案。同一题可以有多个被接受的答案，判题时匹配其中任意一个都算通过。

### 时间和内存

计时从进程创建成功开始，到第一次观测到进程退出为止。插件自己的开销不算在你的程序头上：
`Popen`、抽干管道、读 stderr、停内存采样器都在计时区间之外。喂 stdin 的时间算你的，
评测机上那也是程序在读。串行和并行用同一套口径，同一个二进制不会一边 TLE 一边 AC。

时限是严格比较的。唯一多出来的是最多 50 ms 的观测余量，它决定"多久之后动手杀"，
不决定程序能跑多久。

内存取进程峰值。Windows 用 `GetProcessMemoryInfo`，Linux 读 `/proc/<pid>/status` 里的
`VmHWM`，macOS 用 `libproc.proc_pid_rusage`。超限判 `MLE`。

超时由看门狗终止整个进程树，死循环和它 fork 出来的子进程都会被收掉。

用了时限 90%–99% 的通过会有一条状态栏提醒。只提醒，不改判定。

### 运行错误定位

程序崩溃时，插件先在输出里找位置：Python 回溯、Java 栈、`-fsanitize` 的诊断都能解析出
`文件:行`，卡片和状态栏都会显示，点一下跳到源码。

C++ 的裸段错误本身不带行号。这种情况插件会用 gdb 回放崩溃现场，从调用栈里找属于你自己
的那一帧。回放需要调试符号，所以 Release 构建只给得出 `main ()` 这样的函数名；
`Ctrl+Alt+G` 切到 Debug 再跑，同一个段错误能定位到行。

Debug 模式加的是
`-g -fsanitize=undefined -fsanitize-trap=undefined -D_GLIBCXX_ASSERTIONS`。用 trap 形式
是因为它不需要运行库：MSYS2 的 mingw 没带 `libubsan`，普通的 `-fsanitize=undefined`
会链接失败。有符号溢出、栈数组越界、除零会变成非零退出码判 `RE`，`std::vector` 越界由
断言宏报出来。

堆越界不在默认覆盖范围内。需要的话自己往 `compile_cmd` 里加 `-fsanitize=address`，
前提是工具链有 `libasan`。

### 编译

编译有缓存：源文件（含本地 `#include "..."`）和编译命令都没变时跳过编译，
`Ctrl+Alt+Shift+R` 强制重编。

默认从 `std_floor`（`c++17`）开始编，只有编译器说需要更高标准时才升档，编得过的档位
记在测试数据旁边的 `<名>__tests.std` 里。这样做有具体原因：解析 `<bits/stdc++.h>` 的
耗时随标准上升，开发机上 `-fsyntax-only` 实测 c++17 1.29 s、c++20 2.43 s、c++23 2.53 s，
写死 c++23 等于每次重新编译多等一秒多。普通的语法错误不会触发重试，报错原样给你。

多文件编译用 `extra_sources`（glob）和 `include_dirs`，在编译命令里用
`{extra_sources}` / `{include_dirs}` 占位。

## 详情页

`详情` 打开的是一个真实的编辑器标签页，文字可以选中、复制。里面有：

- 输入、期望输出、实际输出、差异、信息、标准错误输出，每块一张可折叠的卡片；
- 输入和答案的行数与字节数；
- 配了 `subtasks` 时，这个测试点属于哪一组、那组得了多少分；
- 文件输入输出题会写明通道，例如 `add.in -> add.out`。

每个测试点的运行历史仍然记在 `<名>__tests.hist` 里（保留最近 20 次），详情页不显示
这些派生数字。

点区块名折叠或展开。重跑后自动刷新时会保留折叠状态。

颜色用 region 和卡片头实现，不改缓冲区内容：选中、复制、粘出去的文字和看到的一致。
设 `"detail_rich": false` 回到无卡片、无颜色的纯文本详情；`"detail_style": "phantom"`
是旧的嵌入式面板，那种模式下文字不能选中。

详情页和运行面板共用右侧那一列，列宽由运行面板的卡片决定。运行面板的宽度跟着内容走：
卡片放不下时会加宽，内容变短时也会收窄，上下限是 8% 窗口宽和 `max_panel_width_ratio`
（默认半个窗口）。想读宽一点就直接拖，拖过的宽度不会被自动收回。

## 文件输入输出（NOI / CSP-S）

有些题要求 `freopen("add.in", "r", stdin)`、结果写到 `add.out`，用管道喂样例跑不起来。
这类题按题设置一次即可：`View → Algorithm Competition Assistant → This problem
reads/writes files`，两个文件名会按源码名预填（`add.cpp` → `add.in` / `add.out`），
通常连按两次回车。

设置存在测试数据旁边的 `<名>__tests.io` 里，只对这一题生效，重启不丢。

运行时，样例被写进临时目录的 `add.in`，程序也在那个目录里启动，所以 `freopen` 找得到；
答案从程序写出的 `add.out` 读回来判。你的题目目录不会被写入。程序没写出文件时会直接
说明，而不是拿空输出判一个 WA。

限制：文件模式下对拍会拒绝开始。对拍靠管道喂数据，三方程序都按文件读写的话看不到数据，
每轮都会"一致"。要本地对拍就把三份程序改成 stdin/stdout。RE 的 gdb 回放同理，行号仍
可用，文件读取会失败。

另外，`run_cmd` 里如果用相对路径调产物（`./a.out`），文件模式下会找不到——程序是在
临时目录里启动的。默认设置用的是 `{source_file_dir}` 绝对路径，不受影响。

## 对拍

准备三个文件，放在同一目录：

- 你的程序：当前打开的文件；
- 标准程序：暴力或题解，默认找 `std.cpp`；
- 生成器：往 stdout 打印一组随机数据，默认找 `gen.cpp`。

在源码视图按 `Ctrl+Alt+S`。正解和生成器从哪来由 `stress_file_source` 决定：

- `pick_once`（默认）：同目录正好有一对就先用它，不问；没有才第一次问你，选完记在
  `<名>__tests.stress`，以后直接开跑；
- `same_dir`：只在同目录按名字找，`std` / `brute` / `bf` / `sol` / `暴力` 配 `gen` /
  `data` / `maker` / `rand` / `生成`，不弹窗；找不到会告诉你查了哪个目录；
- `auto`：同目录找不到就用设置里手填的 `stress_std_file` / `stress_generator_file`
  （这两个键插件不自动写入），仍不确定才问。

选到同一个文件会被拒绝：程序跟自己比，每轮必然一致。

结果在 `<文件> -stress` 页面里滚动显示。发现不一致就停下，打印输入、两边的输出和逐行
差异；跑满轮数则报告通过。`Ctrl+Alt+Shift+S` 停止当前页这一场，立即生效。多个文件可以
同时对拍，各自有页面。

生成器有独立时限（`stress_generator_time_limit_seconds`，默认 10 秒）。造数据通常比
解题慢，用程序时限卡它会把"生成器超时"误判成失败。超时会跳过该轮，连续 3 次才停。

配了 `checker` 时由 checker 判定，多解题和浮点题不会被逐行比对误报；没配时
`float_tolerance` 生效。

## Competitive Companion

装好浏览器扩展，Sublime 启动时就会在 `12345` 端口监听（`companion_auto_listen`，
默认开），不用先打开文件。扩展的端口列表里要加入 `12345`，这一步在浏览器里，插件
代劳不了。

一次点击送去哪儿，有两种：

- `Ctrl+Alt+L`：按题面新建 `<题名>.cpp`，存好样例和限制，然后停下——不编译、不跑。
  代码还没写，跑没有意义；
- `Ctrl+Alt+F`：把样例送进当前文件并立刻跑一遍。

`companion_target` 决定默认行为，右键菜单和命令面板里两项都有。`new_file` 模式下
"固定目标文件"不生效，一题一个文件就是这个模式的意思。

列表页要注意：AtCoder、洛谷的题目列表页点一次会把整包题目都发过来（实测 7 个请求）。
这种页面用 `Ctrl+Alt+L`；用 `Ctrl+Alt+F` 的话，七道题的样例会全并进当前那个文件。
同一文件的重复自动运行会合并成一轮（2 秒防抖），不会连编七次。

新文件建在哪，按顺序取第一个能用的：`auto_create_dir` → 执行命令时那个 cpp 的目录 →
当前文件所在目录 → 工作区第一个文件夹。注释头可以用 `auto_create_header` 自定义，
占位符是 `{problem}` `{url}` `{time}` `{memory}`。

点扩展没反应时，打开 `companion_debug_log` 再点一次，日志在
`Data/Log/aca-companion.log`。日志里有 `refused 403 origin=…` 说明请求到了但被本地鉴权
挡了；日志完全没动，多半是扩展的端口列表里没有 `12345`。

## 交互题

自动交互（`interactor`）之外还有一个手动终端：`Ctrl+Alt+Shift+X` 打开一个 scratch 视图
运行当前程序，输出实时上屏，`Enter` 发一行输入，`Ctrl+D` 发 EOF，`Ctrl+X` 停程序。
自己当交互器的场合用这个。

`interactor` 按 testlib 约定接：`interactor <输入> <输出> <答案>`，从 stdin 读选手输出、
往 stdout 写发给选手的内容，退出码即判决。

## 其他工具

- Benchmark（`Ctrl+Alt+Shift+K`）：当前测试点跑 N 次，报最快、平均、最慢。
- 机器校准（命令面板 `Calibrate this machine's speed`）：算出本机相对参考评测机的速度
  系数，状态栏显示等效时限。判题仍按你设的原始时限。
- 练习统计（`Show practice statistics`）：每题的尝试次数、首次 AC、判题分布、对拍轮数。
  数据在本插件自己的文件里。
- 比赛计时器（`Ctrl+Alt+Shift+T`）：状态栏倒计时，30 / 10 / 5 分钟各提醒一次。
- 模板片段（`Ctrl+Alt+T`）：光标停在关键字后展开，内置 fastio / main / bf / debug /
  testlib。
- 环境自检（`Ctrl+Alt+D`）：编译器、端口、路径、资源，另附一段可以粘进 issue 的
  Markdown 报告。

## 快捷键

绑定随包加载，不写进你的 keymap；禁用或卸载插件时按键会自动还回去。`Ctrl` 和 `Alt`
都是字面按键，macOS 上是 Control + Option（包里没有 `super+`）。设
`"enable_keybindings": false` 可以整体让出键位。

| 按键 | 功能 |
| --- | --- |
| `Ctrl+Alt+B` | 运行测试 |
| `Ctrl+Alt+Shift+B` | 跑完全部，失败也继续 |
| `Ctrl+Alt+R` | 只重跑失败的测试点 |
| `Ctrl+Alt+Shift+R` | 强制重编后运行 |
| `Ctrl+Alt+Shift+P` | 并行跑全部（`parallel_workers`，默认 4） |
| `Ctrl+Alt+Shift+X` | 交互终端 |
| `Ctrl+Alt+I` | 从文件导入测试 |
| `Ctrl+Alt+V` | 用剪贴板新增测试点 |
| `Ctrl+Alt+E` | 导出为 `.in` / `.out` |
| `Ctrl+Alt+C` / `Ctrl+Alt+Shift+C` | 复制期望 / 实际输出 |
| `Ctrl+Alt+M` | 键盘选测试点，再选操作 |
| `Ctrl+Alt+S` | 开始对拍 |
| `Ctrl+Alt+Shift+S` | 停止当前对拍页 |
| `Ctrl+Alt+L` | Companion：一题一个新文件 |
| `Ctrl+Alt+F` | Companion：送进当前文件并运行 |
| `Ctrl+Alt+Shift+L` | 停止 Companion 监听 |
| `Ctrl+Alt+Shift+F` | 从 URL 抓题 |
| `Ctrl+Alt+Shift+V` | 题面转 Markdown |
| `Ctrl+Alt+Shift+K` | Benchmark 当前测试点 |
| `Ctrl+Alt+Shift+T` | 比赛计时器 |
| `Ctrl+Alt+G` | Debug / Release 切换 |
| `Ctrl+Alt+D` | 环境自检 |
| `Ctrl+Alt+J` | 跳到第一个错误位置 |
| `Ctrl+Alt+T` | 展开模板片段 |
| `Ctrl+Alt+P` / `Ctrl+K, Ctrl+P` | 收起 / 恢复运行面板 |

在插件的测试编辑和详情视图里（`source.TestSyntax`）：`Enter` 插入行，`Ctrl+Enter`
新建测试，`Ctrl+V` 粘贴，`Ctrl+X` 停进程，`Ctrl+D` 删除测试，`Ctrl+Shift+Up/Down`
交换顺序。

## 设置

四个面板改的就是设置文件里那几个键，改完立刻生效：

| 入口 | 管什么 |
| --- | --- |
| `Verdict rules` | 浮点容差、PE 是否算 AC、第一个失败是否停、判题时是否忽略 stderr |
| `Stress test options` | std / gen 从哪来、挑文件方式、反例是否存成测试点、轮数、每轮时限 |
| `Competitive Companion options` | 默认送哪儿、启动是否自动监听、是否记录点击日志 |
| `This problem reads/writes files` | 这一题按 stdin 还是按文件，以及文件名 |

`companion_auto_listen` 是其中唯一要下次启动监听才生效的一项；端口同理，只在启动监听时
读一次。

设置示例：

```json
{
	"language": "zh",
	"run_settings": [
		{
			"name": "C++",
			"extensions": ["cpp"],
			"compile_cmd": "g++ \"{source_file}\" {extra_sources} {include_dirs} -std=c++17 -O2 -o \"{file_name}.exe\"",
			"run_cmd": "\"{source_file_dir}/{file_name}.exe\" {args}",
			"time_limit_ms": 2000,
			"memory_limit_mb": 256
		}
	],
	"float_tolerance": 0,
	"max_output_bytes": 8388608
}
```

其余设置项的注释都在 `Algorithm Competition Assistant.sublime-settings` 里。

## 界面语言

默认中文，`"language": "zh"`。切英文的入口有三处：`Tools → Algorithm Competition
Assistant → Switch language (中/EN)`、命令面板里的 `Switch to English`、右键菜单。
状态栏、弹窗、面板文案和命令面板里的命令描述会跟着变；菜单项是静态 JSON，不跟语言，
文案一律英文——例外是语言切换和中文说明两个入口，特意带上中文标注，方便中文用户找得到。

界面切到英文后，详情页 `[信息]` 区块里插件自己写的判决附言（RE 位置、输出截断提示、
TLE 原因）可以仍然用中文：设 `"message_language": "zh"`（默认 `"auto"`，跟随界面）。
这些话是"下一步该看哪里"的结论，读错比读慢更伤。checker 程序自己打印的原文不受影响。

## 已知限制

- macOS 的内存是单进程采样得到的峰值，不是内核统计的峰值 RSS，可能比活动监视器略低。
- Release 下裸段错误的行号只能给到函数名；要行号就切 Debug，或自己加 `-g`。
- `sync_output` 默认关闭。开启后每个字符刷新一次视图，只适合交互程序。
- stderr 只在详情页显示，卡片上不挂。
- 空测试点（没有输入也没有答案）不存盘，它只是接收粘贴的占位。
- 计时从进程启动开始；还没粘贴样例时，计时和看门狗都停着，从第一个字符或第一行输出
  开始算。
- 程序崩溃时优先判 `RE`：退出码带崩溃信号，或输出里有崩溃特征，即使正好被超时杀掉也判
  RE。普通的 `cerr` 输出不算。
- `PE` 只在 token 完全相同、空白不同时给出。OJ 把 PE 也算通过的话，设
  `regard_pe_as_ac`。
- 输出超过 `max_output_bytes`（默认 8 MB）会被截断并提示。
- 抓题有体积和超时上限（`max_fetch_bytes` 8 MB、`fetch_timeout_seconds` 25 秒）。
  靠脚本渲染的题面会解析成空，提示"可能需要在浏览器里登录"。
- Windows 上编译器按 ANSI 代码页写出的非 ASCII 文件名，和磁盘上的实际名字可能不一致。
  插件编译后会核对产物、按真实文件名运行，并在控制台说明。
- 文件模式下对拍不可用，gdb 回放读不到输入文件。
- 插件不热重载：改 `.py` 后要重启 Sublime。

## 开发

- 插件宿主是 Python 3.3，代码不能用 f-string、`subprocess.run`、
  `Popen(encoding=…)` 这些新版本的东西，`tests/check_py33.py` 会拦。
- 回归测试：`python tests/run_tests.py`，当前 1044 项，0 失败。测试用假的 `sublime`
  模块，不需要装 Sublime。
- CI 跑 Linux（3.9 / 3.12）、Windows、macOS。
- 版本号在 `plugin_init.py`，发版要同步 `messages.json` 和 `messages/<版本>.txt`，
  `tests/check_messages.py` 会检查。

## 致谢

FastOlympicCoding（Jatana）是这个插件的基础；判题结果的显示方式参考了 cph-ng
（langningchen）；Competitive Companion 的接入参考了 FastOlympicCodingHook（DrSchwad）。
MIT 协议，见 `LICENSE`。
