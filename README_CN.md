# Algorithm Competition Assistant 使用说明（中文）

> 英文简介：[README.md](README.md)

**Sublime Text 上更「能打」的算法竞赛测评插件** —— 一个测试点卡片 + 彩色 verdict + 逐行 diff 详情 + 真实内存/超时判定的完整闭环。

> **基于什么开发**：本插件是 **[FastOlympicCoding](https://github.com/Jatana/FastOlympicCoding)**（Jatana）的深度二次开发版本，
> 参考 VSCode 的 **[cph-ng](https://github.com/langningchen/cph-ng)** 重做了判题结果显示（彩色 verdict、详情面板、逐行 diff），
> 并借鉴 **[FastOlympicCodingHook](https://github.com/DrSchwad/FastOlympicCodingHook)** 集成了 [Competitive Companion](https://github.com/jmerle/competitive-companion) 浏览器插件支持。

---

## 相比原版 FastOlympicCoding，多了什么

| 能力 | FastOlympicCoding | 本插件 |
| --- | --- | --- |
| 彩色 verdict 徽章（AC/WA/TLE/MLE/RE/PE/CE…） | ❌ 只有纯文本 | ✅ 类 cph-ng 的徽章样式 |
| **真实内存判定（MLE）** | ❌ 内存是死配置 | ✅ 跨平台采样进程峰值内存，超限真判 MLE |
| **超时硬杀（TLE）** | ❌ 死循环杀不掉 | ✅ 看门狗到点终止整个进程树 |
| **运行错误定位（RE 位置）** | ❌ | ✅ 从程序输出解析 `文件:行号`（Python 回溯 / Java 栈 / sanitizer 诊断），解析不到就用 gdb 回放崩溃现场 |
| 详情视图 + **逐行 diff** | ❌ | ✅ 独立标签页，可选中可复制；含时限/内存用量、与上次对比、子任务归属 |
| **编译缓存** | ❌ 每次都重编 | ✅ 源文件/命令未变则跳过编译 |
| **编译标准自动档（少等 1.4~2 秒/次）** | ❌ 写死什么就编什么 | ✅ 低档先编，需要才升，按题目文件记住 |
| 只重跑失败 / 跑完全部 / 强制重编 | ❌ | ✅ 三个命令 + 快捷键 |
| **浮点容差**（`float_tolerance`） | ❌ | ✅ 浮点题不再因 `0.1+0.2 != 0.3` 误判 |
| 输出体积上限（防卡死） | ❌ | ✅ `max_output_bytes` |
| 多文件编译（`extra_sources` / `include_dirs`） | ❌ | ✅ |
| 环境自检 doctor | ❌ | ✅ 一条命令排查 + 可粘贴的 Markdown 报告 |
| 纯键盘操作（选测试点→操作） | ❌ | ✅ `Ctrl+Alt+M` |
| 对拍反例自动入库 | ❌ | ✅ |
| **文件输入输出（NOI / CSP-S 复赛的 freopen 题）** | ❌ 只认管道 | ✅ 按题设置读写文件名，样例写进临时目录，答案从 `add.out` 读回来判 |
| Competitive Companion 持久监听 | ⚠️ 需 Hook | ✅ 启动即监听、按题面自动建文件 |
| 中英文双语界面 | ❌ | ✅ |
| 测试面板自适应宽度 | ❌ | ✅ 卡片放不下时自动加宽 |

工程上还有：**Python 3.3 兼容**（Sublime 插件宿主）、四平台 CI（Linux 3.9/3.12 + Windows + macOS）、**876 项回归测试**。

---

## 主要功能

### 1. 类 cph-ng 的测评结果显示

每个测试点显示：

- **彩色 verdict 徽章**：`AC`（绿）、`WA`（红）、`TLE`（黄）、`MLE`（紫）、`RE`（蓝）、`PE`（粉）、`CE`（黄）等
- **运行时间**：毫秒级，超过 5 秒自动换算成秒
- **内存占用**：MB / GB（**跨平台真实采样进程峰值内存**：Windows 用 `GetProcessMemoryInfo`、Linux 读 `/proc/<pid>/status` 的 `VmHWM`、macOS 用 `libproc.proc_pid_rusage`），超限会真的判 `MLE`
- **超时硬杀**：超过时间限制会被自动终止并判 `TLE`，不会出现死循环杀不掉；手动停止的测试显示 `SK`
- **运行错误定位**：程序崩溃（`RE`）时，若输出里有位置信息（Python 回溯、Java 栈、`-fsanitize` 诊断），卡片详情会显示 `运行错误 (RE) 位置: main.py:12`，状态栏同时提示
- **三个按钮**：`编辑`、`运行`、`详情`

> **C++ 想拿到 RE 行号？** 普通的段错误本身不带行号。用 `Ctrl+Alt+G` 切到
> **Debug 模式**再跑一次：Debug 会加上 `-g -fsanitize=undefined
> -fsanitize-trap=undefined -D_GLIBCXX_ASSERTIONS`，有符号溢出、栈数组越界、除零
> 会直接变成非零退出码（判 `RE`），`std::vector` 越界由断言宏给出 `stl_vector.h:1263`。
> 若程序是裸段错误、输出里没有行号，插件会自动用 **gdb 回放**崩溃现场
> （`bt` + `info locals`）找出你自己的那一帧，行号照样可以点着跳过去。
> 只有崩溃发生在 Debug 也没覆盖到的地方（例如堆越界）才需要手动把
> `-fsanitize=address` 写进 `compile_cmd`——注意本机 MSYS2 mingw 没有
> `libasan`/`libubsan`，写进去会链接失败。

### 2. 详情视图（detail）+ 逐行 diff

点击 `详情` 打开一个真实的编辑器标签页（`xxx - test N detail`），文字**可选中、可复制**：

- **预期输出** / **实际输出** / **错误输出 (stderr)**
- **Diff**：预期与实际**逐行对比**（忽略行末空格与末尾换行），只列出有差异的行
- 顶部显示 verdict + 运行时间 + 内存占用
- **时限与内存用量百分比**：`时限用量 1200ms/2000ms = 60%`。AC 但用了 96% 是个坑，
  只看 verdict 看不出来
- **和上一次比**：`与上次 300ms -> 1200ms (4.00x)`，三次以上再给 `历史 最快/平均/最慢`。
  "我这次改动到底有没有让它变快"以前没有答案——每次运行都会覆盖掉上一次的数字，
  现在每个测试点的历史存在测试数据旁边的 `<名>__tests.hist`（每题最多留 20 次），
  串行、并行、Benchmark 都会写
- **输入/输出的行数与字节数**：两万行的样例和两个数的样例是两种阅读负担，标签上先说明白
- **属于哪个子任务、那组拿了多少分**（配了 `subtasks` 时）
- **评测通道**：文件输入输出的题会写明 `文件输入输出 add.in -> add.out`，
  因为最常见的"详情里什么都没有"就是 freopen 名字和题面不一致

颜色是 **region + 顶部 phantom 徽章条**，不是语法着色：**缓冲区一个字节都不改**，
选中、复制、粘出去的内容和没有颜色时完全一样（自定义语法当初是特意撤掉的）。
徽章条上还带 `复制预期输出` / `复制实际输出` / `✕ 关闭`。不想要就设
`"detail_rich": false`，详情页回到今天这个样子。

运行结束后再次点击 `详情` 会自动刷新。想看颜色版又不用离开面板，可以设
`"detail_style": "phantom"`（旧的嵌入式面板，文字不可选中）。

### 3. 正确/错误答案快捷标记

运行结束后输出下方出现 `accept` / `decline`：`accept` 把当前输出记为正确答案，`decline` 记为错误答案。

### 4. 集成 Competitive Companion 浏览器插件

**装好就能用**：Sublime 一启动就自动在 `12345` 端口监听（设置项 `companion_auto_listen`，
默认开），不用先打开或保存任何文件。

1. 浏览器打开题目页，点 Competitive Companion 扩展的绿色 `+`
2. 一次点击送去哪儿由 `companion_target` 决定，两种行为是**分开的**（右键、View 菜单、
   命令面板和快捷键都各有两项）：
   - `Ctrl+Alt+L` / `... new file per problem`（默认）：按题面新建 `<题目名>.cpp` 并打开，
     存好样例、记住时间/空间限制，然后**就此为止**——不编译、不弹运行面板、不跑样例
     （代码还没写，跑了只是白占内存）。重名就打开已有那个再把样例并进去，不会越点越多。
   - `Ctrl+Alt+F` / `... fill the current file`：把样例送到当前聚焦（或你固定过）的源码文件，
     并立刻跑一遍——这个文件里已经有代码，跑才有意义。
   - `new_file` 模式下**固定目标不再生效**：一题一个文件就是这个模式的全部意思。
     （以前它被固定项压住，于是"在 std.cpp 里开过监听"之后整个会话都只往那个文件送，
     看起来就像新建功能完全没用——真实日志里出现过这个。）
3. 新文件建在哪，按顺序取第一个能用的：`auto_create_dir` → 你在某个 cpp 里点
   `new file` 那项时那个 cpp 所在的目录 → 当前聚焦的源码文件所在目录 → 工作区第一个文件夹。
   所以在一题一目录的比赛文件夹里点一下，后面每道题都建在同一个目录里。

**一次点击不等于一道题**：AtCoder、洛谷这类**题目列表页**会把整包题目一起发来（实测一次点击
= 7 个 POST）。所以列表页请用 `Ctrl+Alt+L`（一题一个文件）；`Ctrl+Alt+F` 只适合单题页面，
否则七道题的样例都会并进你站着的那一个文件。按同一个文件重复自动跑样例也会被折成一轮
（`AUTO_RUN_DEBOUNCE_SECONDS`，2 秒），不会连着编译七次。按键时状态栏就会把这一点写出来。

新建文件顶部的注释头可以整条换掉：`auto_create_header` 写字符串或"每行一个元素"的数组，
可用 `{problem}` `{url}` `{time}` `{memory}`，注释符号也由你写（内置默认会按扩展名选
`//` 或 `#`，`null` 就用它，`""` 则一行都不写）；`auto_create_template` 是紧随其后的正文模板。

插件**只会往自己新建的文件里写注释头**，你正在写的源码一个字都不会被动。四处目录一个都取不到时
状态栏会直说"先打开题目所在的文件夹"，不会静默丢掉样例。状态栏每次都会**写明样例落进了哪个文件**。

样例的落盘位置与去重规则和「导入」完全一致：按输入去重、答案只补不覆盖，重复点击
不会顶掉你手工标记的答案。收到的时间/空间限制写在测试数据旁边的 `<文件名>__tests.limits`
里，重启 Sublime 也不会丢。

想把样例长期收进某个特定文件：在里面按 `Ctrl+Alt+F`，或执行不带模式的
`Algorithm Competition Assistant: Listen to Competitive Companion`——它把这个文件设为目标
并切到 `current_file`，本会话内的点击都送进它（幂等，不会重复起服务器或撞端口）。想回到
一题一文件，按 `Ctrl+Alt+L` 即可，它会顺手解除固定。`Stop Competitive Companion listener`
（`Ctrl+Alt+Shift+L`）随时停止，停止后目标、模式与目录都清空，要重新监听需再执行一次命令或
重启 Sublime。

`companion_debug_log: true` 会把每次点击的判定过程（模式、样例数、限制、最后落到哪个文件
或为什么被拒）追加到 `Data/Log/aca-companion.log`，用来回答"样例到底去哪了"；默认关。

不想改设置就直接右键，两种行为各一项：

- `Algorithm Competition Assistant: Listen to CC, one new file per problem`
- `Algorithm Competition Assistant: Listen to CC, fill the current file`

选中后本次会话就按它走（不改设置文件），停止监听后回到 `companion_target` 的设置值。

**点了没反应时**：`companion_debug_log` 打开再点一次。日志里有 `refused 403 origin=…`
就是被本地鉴权挡了（把 Origin 发过来告诉我们）；日志完全没动，说明请求根本没从浏览器发出来，
十有八九是扩展的端口列表里没有 `12345`。

> v2.4.11 之前这里的鉴权只认 `localhost` 来源，把扩展自己发请求时带的
> `chrome-extension://<id>` 当成外部页面拒掉了——点击于是变成无声的 403。现在扩展来源
> 放行，非 `localhost` 的 Host 与真实网页的 Origin 仍然拒绝（防 DNS rebinding 和网页乱 POST）。

**注意**： Competitive Companion 扩展的端口列表里要加入 `12345`（可用设置项 `companion_port`
修改）——这一步在浏览器里，插件代劳不了。

### 5. 国际化 (i18n)

默认中文，可切换：

- **菜单**：`Tools` → `Algorithm Competition Assistant` → `Switch language (中/EN)`
- **命令面板**：`Algorithm Competition Assistant: Switch to English` / `切换为中文`
- **右键菜单**：`Switch language (中/EN)`
- **设置**：`"language": "en"` 或 `"zh"`

### 6. 运行模式与效率

- **编译缓存**：源文件（含多文件依赖）与编译命令没变时跳过编译；需要时 `Ctrl+Alt+Shift+R` 强制重编
- **编译标准自动档**（`auto_std_switch`，默认开）：先用 `std_floor`（默认 `c++17`）编，只有编译器自己
  说"这个语法要 C++NN 才有"（或源码里写了 `#include <expected>` / `std::println` 这类新标准才有的东西）
  才升档，并把这道文件编得过的档位记在 `<名>__tests.std` 里。写死 `-std=c++23` 的代价是**每次 Run 白等
  1.4~2 秒**（实测解析 `<bits/stdc++.h>`：c++17 1.29s，c++20 2.43s，c++23 2.53s；整份题解完整编译
  1.69s vs 3.12s）。普通拼写/语法错误**不会**触发重试，报错原样给你；`Ctrl+Alt+Shift+R` 会把档位忘掉
  从头试，`auto_std_switch: false` 则完全按你命令里那一档编。预编译头试过，更慢（3.12s→3.46s，
  还要 151 MB 的 .gch），所以没做
- **只重跑失败 / 跑完全部**：`Ctrl+Alt+R` 只重跑没 AC 的点，`Ctrl+Alt+Shift+B` 跑完全部（默认第一个失败即停，可用 `stop_on_first_failure` 改）
- **浮点容差**：设置 `float_tolerance`（如 `1e-6`），数字型输出按相对/绝对误差比较
- **输出上限**：`max_output_bytes`（默认 8MB）防止疯狂输出卡死编辑器，超出部分会被截断并给出提示
- **多文件编译**：`run_settings` 里用 `extra_sources`（glob）与 `include_dirs`，编译命令中用 `{extra_sources}` / `{include_dirs}` 占位符
- **面板汇总行**：底部显示 `4/5 通过 · 首个失败 test 3 · 总用时 1.24s`
- **自定义判定器（SPJ）**：`run_settings` 里填 `checker` 即可用 testlib 风格的 checker（三个文件参数 + 退出码判定），多解题不再只能靠 token 比较
- **交互题**：填 `interactor` 即按 testlib 约定接线，选手程序与 interactor 双向对话，退出码即判决
- **Subtask 分组**：`subtasks` 里分组，组内全 AC 才拿分，汇总行显示部分分
- **Benchmark / 机器校准 / 练习统计 / 比赛计时 / 抓题与题面预览 / 保存自动运行**：命令面板里搜 `Algorithm Competition Assistant:` 都能找到
- **错误位置可点击**：RE 的 `文件:行:号` 和编译器的每条诊断都会渲染成链接，点一下直接跳到源码那一行（已经打开的标签会被复用，不会新开一个）
- **Debug / Release 一键切换**（`Ctrl+Alt+G`）：Debug 去掉优化并加 `-g -fsanitize=undefined -fsanitize-trap=undefined -D_GLIBCXX_ASSERTIONS`，有符号溢出、数组越界、除零都会变成非零退出码并判 RE，行号由 gdb 回放给出、可以点着跳过去；Release 保证 `-O2` 并去掉 sanitizer 与断言，计时与评测机可比。按文件记忆，**不写你的设置文件**；当前模式显示在运行面板状态栏
- **并行运行**（`Ctrl+Alt+Shift+P`）：每个测试点一个独立进程、`parallel_workers`（默认 4）个 worker 同时跑。串行时 5 个点里有 2 个 TLE 要等 2×时限，并行约 1/4
- **环境自检**：`Ctrl+Alt+D` 检查编译器是否在 PATH、端口占用、测试路径可写、资源可加载，
  并**真的执行一次工具链**（跑 `g++ --version`，再用你自己的 compile/run 命令编译并运行一个
  小程序——缺运行库 DLL、32/64 位错配、WSL 桩、被杀软拦截这些"在 PATH 上但跑不起来"的情况
  只查 PATH 是查不出来的），另外检查 gdb / clang-format / checker / interactor 文件是否到位，
  `<bits/stdc++.h>` 缺失会单独说明。探测在报告已显示之后于后台线程进行，命令本身秒开，
  并给出一段可粘贴到 issue 的 Markdown 报告（含插件版本号与实际安装目录名）
- **纯键盘流**：`Ctrl+Alt+M` 选测试点 → 运行 / 详情 / 编辑 / 接受 / 拒绝 / 删除
- **跑完直接去错误现场**：底部汇总行的 `首个失败 test 3` 现在能点，点一下打开那个测试点的
  详情页；`Ctrl+Alt+J` 从源码文件跳到本次运行的第一个编译诊断 / RE 行号（跳转目标存在运行面板上，
  以前"站在 main.cpp 里按这条命令"会因为只检查当前视图而永远是灰的）。命令面板给两个：
  `Jump to the first error location`（g++ 的第一条才是根因，后面几百条是连锁反应）和
  `Jump to the last error location`
- **文件输入输出（NOI / CSP-S 复赛）**：见上面「7. 文件输入输出」一节，按题设置读写文件名，
  样例写到临时目录再读回答案，绝不碰题目目录里的真数据
- **对拍反例入库**：对拍发现反例自动保存为正式测试点（可用 `stress_save_counterexample` 关闭）
- **对拍收尾统计**：结束时报 `本轮统计: 共 N 轮, 实际比较 M 轮, 生成器超时 K 次`——`Passed: N rounds` 只说明跑了多少轮，不说明有多少轮真的比出结果
- **统一合并策略**：导入文件 / 剪贴板 / 浏览器 / 对拍反例四条路径共用「按输入去重、答案只补不覆盖」，重发样例不会顶掉你手动标记的答案

### 7. 文件输入输出（NOI / CSP-S 复赛那种）

复赛题目会写"从文件 `add.in` 中读入数据，输出到文件 `add.out`"，代码里要自己
`freopen`。插件默认把样例从**管道**喂进 stdin、从 stdout 读答案，那种题本地跑不
起来——以前只能把代码改回 `cin/cout` 再测，于是"测过的版本"和"要交的版本"不是
同一份。

现在按题设置：`View → Algorithm Competition Assistant → This problem reads/writes
files`（或命令面板搜 `file`，也可以按住快捷键以外的入口——这项没有默认按键），
两次输入都**预填好**（默认按源码文件名猜：`add.cpp` → `add.in` / `add.out`），
通常连按两次回车就完事。设置存在测试数据旁边的 `<名>__tests.io` 里，
和 `.limits` / `.std` / `.stress` 同一套做法：**只跟着这一题**，重启不丢，
不会因为你给 A 题设过就把 B 题也拖进文件模式。

跑起来之后：

- 样例被写成运行目录里的 `add.in`，程序就在那个目录里启动，所以 `freopen("add.in", "r", stdin)`
  一定找得到；**你的题目目录一个字节都不会被动**（那里的真数据文件是评测要用的，
  覆盖掉它比判错更糟）
- 答案从程序写出的 `add.out` 读回来判定，stdout 只当调试输出（照样抽干，
  免得打印多的程序把自己堵死在管道上）
- 如果程序压根没写出 `add.out`，不会给你一个空白的 WA，而是直说"没写出这个文件，
  去检查 freopen 和文件名对不对"（题面写 `.ans` 就别写 `.out`）
- 运行面板状态栏会带上当前方式：`C++ · TL 1000ms · ML 256MB · 文件输入输出 add.in->add.out`
- 并行跑全部（`Ctrl+Alt+Shift+P`）与 Benchmark 走同一套规则，不会串行按文件、并行按管道

**限制**：对拍（`Ctrl+Alt+S`）在文件模式下会**拒绝开始**并说明原因——对拍是把数据
从管道喂进去的，按文件读写的 std/gen/你的程序三方都看不到那份数据，每一轮都会"一致"，
那种通过是自欺欺人。要本地对拍就把三份程序改成 stdin/stdout。RE 的 gdb 回放同理，
回放时是按管道的，行号仍可用但文件读取会失败。

### 8. 测试面板自适应宽度

右侧运行面板默认只占窗口 32%；卡片按钮放不下时，插件会在每次刷新后自动加宽，直到最宽的卡片能一行放下：

- **按面板上实际卡片的内容测量宽度**（真实 verdict / 运行时间 / 内存芯片），加宽到刚好够所有卡片单行放下为止，不再按"最坏可能的卡片"多让出一截
- **只在需要时加宽**，从不自动收窄
- **上限为半个窗口**（可配置）

```json
{
	"auto_fit_panel_width": true,
	"max_panel_width_ratio": 0.5
}
```

卡片默认只显示数值本身（`2542ms | 4 MB`，单位已经说明含义）。想要 2.9.1 那样的
`time: 2542ms | mem: 4 MB` 前缀（中文界面为"时间: / 内存:"）：

```json
{
	"show_time_mem_labels": true
}
```

## 安装

**推荐：直接把仓库文件夹放进 `Packages`（文件夹安装）**

1. 克隆或下载本仓库（`chenmoulaile/algorithm-competition-assistant`）
2. 把仓库文件夹放进 Sublime Text 的 `Packages` 目录，目录名随你——v2.4.11 起路径按插件真实所在目录
   （或 zip 名）推导并去掉 `.sublime-package` 后缀，所以叫什么都一样；数据文件名也保持不变
3. 重启 Sublime Text

放完就是全部——`Packages/Algorithm Competition Assistant` 这个文件夹**就是**插件本身。
改了 `.py` 要重启 Sublime 才生效（菜单、快捷键、设置这些 JSON 更要重启），所以想要
"改完立刻生效"的体验，就把这里当唯一的一份，别再让 Package Control 装第二份。

**Package Control（已收录进官方市场）**

1. `Ctrl+Shift+P` → `Package Control: Install Package`
2. 搜 `Algorithm Competition Assistant` 回车

装完即生效，无需重启，日后升级用 `Package Control: Upgrade Package`。

**配置 Package Control 安装。** Package Control 装的是 zip，`Packages/` 下没有插件文件夹，不用去找目录。
用 `Preferences → Package Settings → Algorithm Competition Assistant → Settings`：左边是只读默认值，右边是可编辑的
`Packages/User/Algorithm Competition Assistant.sublime-settings`，保存后升级不丢。同一个子菜单里的 Key Bindings、
TestSyntax Settings 和两份 README 也能直接打开。

⚠️ 两种方式**不要同时用**，而且这里有个坑值得单独讲：Package Control 装的是
`Installed Packages/…sublime-package`（zip）。如果 `Packages/` 下同时有一个同名文件夹，
安装/升级 zip 版的那一步会把**那个文件夹清空**——v2.5.0 之前这件事发生的时候一点动静都没有，
症状是"我改了代码、重启，什么都没变"，而 `Packages/Algorithm Competition Assistant` 只剩一个
空目录（实测：目录还在，创建时间是一次重启之后，内容 0 个文件）。
现在启动时会打印并状态栏提示"检测到两份安装 / 目录已被清空"，环境自检（`Ctrl+Alt+D`）也写明当前
加载的是 `folder` 还是 `zip`、版本号是多少。

想用 Package Control 的自动升级，就让它只有一份：把源码检出目录移出 `Packages/`（放别处当开发用即可）。
想自己改代码、改完重启就是你自己的版本，就用文件夹安装，别再装 Package Control 的那一份。

## 使用方法

1. 打开 C++ 源文件
2. 按 `Ctrl+Alt+B` 启动测评
3. 右侧打开测试运行窗口，可输入/编辑测试数据
4. 测评结束后每个测试点显示 verdict 徽章
5. 点 `详情` 看带逐行 diff 的详情页；点 `编辑` 打开输入（`test N -edit`）与标准答案（`test N -answer`）两个标签页，在答案页填好预期输出后 `save` 即可自动重新评判

> **关于默认运行命令**：默认 `run_cmd` 用正斜杠路径，Windows / Linux / macOS 通用。想换成自己的写法，把 `run_settings` 复制到 User 设置里覆盖即可。

## 已知限制

- **macOS 内存占用**是单进程实时采样（`libproc.proc_pid_rusage`）的峰值，不是内核严格意义上的峰值 RSS，可能比 Activity Monitor 略低；已不再使用 `RUSAGE_CHILDREN`（所有子进程累计峰值）那种错误口径。
- **裸段错误（C++）的行号看编译模式**：RE 位置优先从程序自己的输出里解析（Python 回溯 / Java 栈 / sanitizer 诊断）；
  解析不到时插件用 gdb 回放崩溃现场。回放要能报出 `文件:行` 得靠调试符号，所以 **Release 下只给出 `main ()` 这样的函数名**
  （实测 gdb 输出 `No symbol table info available`），**`Ctrl+Alt+G` 切到 Debug 后同一个段错误能直接定位到行**
  （实测 `seg.cpp:4`）。想要 Release 也有行号，就自己往 `compile_cmd` 里加 `-g`。
- **`sync_output`（逐字符同步输出）默认关闭**：开启后一个字符刷新一次视图，只适合交互式程序。
- **stderr（`cerr` 调试输出）只显示在详情视图里**，不再挂在卡片上（卡片会变得又挤又长）。
- **"还没粘贴样例"的时间不计时**：新建测试点后如果输入还是空的，程序虽然已经启动，
  但运行计时与 TLE 看门狗都在等你输入——从你粘贴/输入的第一个字符、或程序第一次输出开始才算。
  因此不会出现"样本还没粘完就已经 TLE"。
- **计时只算你的程序**：表在进程创建成功的那一刻才起，在首次观测到进程退出的那一刻就停；
  插件自己的开销（`Popen` 约 36 ms/次、输出抽干、stderr 读取、内存采样器关停）都不记到你头上
  （实测 sleep 300 ms 的程序：判题看到的从 537 ms 变成 343 ms）。串行与并行用同一条规则，
  不会再出现同一个二进制一边 TLE 一边 AC。**但插件绝不比评测机宽松**：时限仍严格比较，
  喂 stdin 的时间仍算你的程序（评测机上那也是），多的 50 ms 只是"我们多久后动手杀"的观测余量。
- **贴线会提醒**：通过但用了时限 90–99% 的运行，状态栏会提示"本机若比评测机快，OJ 上可能 TLE"。
  只提醒，不改判题结果。
- **机器校准显示在状态栏**：跑一次 `Calibrate machine`，运行面板状态栏就会出现
  `本机 1.31× ≈ OJ 2620ms` 这样的等效时限；判题仍按你设置的原始时限。
- **RE 优先于 TLE**：程序崩溃时看判定退出码（访问越界 / 除零 / 栈溢出 / `abort`）和错误输出里的
  崩溃特征（`terminate called after throwing`、`Traceback`、`AddressSanitizer` …），
  即使恰好超时被杀也判 RE；普通的 `cerr` 调试输出不会被误判。
- **空样例不会保存**：没有任何输入也没有答案的空白测试点只存在于当前面板（它是用来接收
  你粘贴样例的占位），不会写进测试文件。
- **`PE`（Presentation Error）**：只有「token 完全相同但空白/换行不同」才算 PE，默认与 WA 分别显示；若你的 OJ 把 PE 也算通过，把 `regard_pe_as_ac` 设为 `true`。
- 输出超过 `max_output_bytes`（默认 8MB）时超出部分会被丢弃，并插入一行截断提示。
- **抓题有页面体积上限**（`max_fetch_bytes`，默认 8MB，可在设置里调整）：`core/cph_fetch.py`
  按 64KB 分块下载页面，累计一超过上限就立即停止并报
  「抓取失败：页面超过 max_fetch_bytes 上限…」（英文环境是
  `fetch failed: the page is larger than the max_fetch_bytes cap ...`），
  超限的页面既不会照单全收，也不会给你半页题面。另一道闸门是超时（`fetch_timeout_seconds`，
  默认 **25 秒**，可在设置里调整）：超时同样按抓取失败报 `抓取失败：…`。慢的 OJ 和挂掉的 OJ
  原本长得一样，所以这个值从硬编码的签名默认值改成了设置项。题面本身不做任何长度裁剪，上限内放得下的
  样例、标题、题面正文有多长就写多长。唯一的例外是状态栏：题面里没有标题时
  （`<title>` 与 `<h1>` 都没有），`summarize()` 会取最长的一行正文压到 160 字当摘要，
  免得状态栏只剩一个光秃秃的 URL、看不出抓的是不是这道题。
  题面转 Markdown 时会丢掉 `<script>` / `<style>` / 注释，所以靠内嵌脚本渲染的题目
  （少数动态 OJ）会解析成空，此时提示「题面解析为空（可能需要在浏览器里登录）」。
- **非 ASCII 文件名靠自动识别**：Windows 上编译器按 ANSI 代码页写出的 `中文.exe`
  在磁盘上会变成另一个名字（GBK 字节被当成 latin-1 读），插件编译后会核对产物、
  按真实的文件名去运行，并在控制台说明（`the binary is ... on disk, not ...`）。
  如果你的编译命令把 `-o` 写到别处，请保证输出名用引号包起来。

## 快捷键

> 绑定随**插件包**一起加载（`Default (<你的平台>).sublime-keymap`），**不会写进你自己的 keymap**。
> 所以禁用或卸载本插件时，这些按键会自动还给其它插件，不会留下"占着键位却没人响应"的死绑定。
>
> - 想临时让出键位：设置 `"enable_keybindings": false`，不用改任何 keymap。
> - 想改某一个键：`View → Algorithm Competition Assistant → Key Bindings`
>   会左右分屏打开包内 keymap 和你对它的覆盖，照抄那一条改按键名即可。
>   同一条绑定在两边同时存在时，**你这一侧赢**。
>
> 下表就是三个平台随包默认生效的绑定：`Ctrl` 与 `Alt` 都按字面算，
> macOS 上是 **Control + Option**（不是 Cmd），包内 keymap 里没有一条 `super+`。
> 面板内的 `Enter / Ctrl+V / Ctrl+D …` 只在本插件的测试编辑/详情视图里生效。

| 按键 | 功能 |
| --- | --- |
| `Ctrl+Alt+B` | 运行测试 |
| `Ctrl+Alt+I` | 从文件导入测试 |
| `Ctrl+Alt+S` | 开始对拍（对拍输出页上也按它 = 重新跑该文件的这场对拍） |
| `Ctrl+Alt+Shift+S` | 停止对拍，**只在对拍输出页生效**；多个文件同时对拍时停的是当前页这一场 |
| `Ctrl+Alt+L` | 监听 Competitive Companion：**一题一个新文件**，不自动跑样例 |
| `Ctrl+Alt+F` | 监听 Competitive Companion：样例送进**当前文件**并立刻跑一遍 |
| `Ctrl+Alt+Shift+L` | 停止 Competitive Companion 监听 |
| `Ctrl+Alt+P` 或 `Ctrl+K, Ctrl+P` | 收缩 / 恢复右侧测试面板 |
| `Ctrl+Alt+T` | 展开模板片段（光标停在关键字后，如 `fastio`） |
| `Ctrl+Alt+V` | 用剪贴板内容新增测试点（`输入 --- 输出` 可同时带答案） |
| `Ctrl+Alt+C` / `Ctrl+Alt+Shift+C` | 复制当前测试的 预期输出 / 实际输出 |
| `Ctrl+Alt+E` | 把全部测试导出成 `1.in` / `1.out` 文件对 |
| `Ctrl+Alt+R` | 只重跑失败的测试点（已 AC 的自动跳过） |
| `Ctrl+Alt+Shift+B` | 跑完全部测试点（不因失败中断） |
| `Ctrl+Alt+Shift+P` | **并行**跑完全部测试点（多进程，TLE 多时总时长约 1/4） |
| `Ctrl+Alt+Shift+F` | 从题目 URL 抓题：样例变测试点（可同时打开题面） |
| `Ctrl+Alt+Shift+V` | 把题目题面转成 Markdown 打开 |
| `Ctrl+Alt+Shift+K` | Benchmark 当前测试点（跑 N 次报最快/平均/最慢） |
| `Ctrl+Alt+Shift+T` | 比赛计时器（状态栏倒计时，可停止） |
| `Ctrl+Alt+Shift+R` | 强制重新编译后运行（忽略编译缓存，并重置这道文件的编译标准档） |
| `Ctrl+Alt+G` | 切换 Debug / Release 编译模式（状态栏显示当前模式） |
| `Ctrl+Alt+D` | 环境自检（编译器 / 端口 / 路径 / 资源） |
| `Ctrl+Alt+M` | 键盘选择测试点并运行 / 详情 / 编辑（不用鼠标） |
| `Ctrl+Alt+J` | 跳到第一个错误位置（编译诊断 / RE 的行号；命令面板里也有"跳到最后一个"） |
| `Enter` (在 TestSyntax 中) | 插入行 |
| `Ctrl+Enter` (在 TestSyntax 中) | 新建测试 |
| `Ctrl+V` / `Cmd+V` (在 TestSyntax 中) | 粘贴 |
| `Ctrl+X` / `Cmd+X` (在 TestSyntax 中) | 终止进程 |
| `Ctrl+D` / `Cmd+D` (在 TestSyntax 中) | 删除测试 |
| `Ctrl+Shift+Up/Down` (在 TestSyntax 中) | 交换测试顺序 |

## 设置面板（改开关不用开 JSON）

赛前最常切的几个开关原来只能改设置文件：打开 JSON、找到键、改字符串或布尔、保存。
现在这四个面板各一屏就能改完，而且**改完立刻生效**（它们都是每次使用时现读的）：

| 命令面板 / View 菜单 | 管什么 |
| --- | --- |
| `Verdict rules` / 判定口径 | 浮点容差、PE 当 AC、第一个失败就停、判定时忽略 stderr |
| `Stress test options` / 对拍设置 | `std`/`gen` 从哪来、挑文件用原生选择器还是快速面板、反例是否存成测试点、最大轮数、每轮时限 |
| `Competitive Companion options` / 监听设置 | 一次点击送到"一题一个新文件"还是"当前文件并跑一遍"、启动是否自动监听、是否记录点击日志、打开日志 |
| `This problem reads/writes files` / 本题评测方式 | 这一题按 stdin 还是按文件（NOI / CSP-S），以及输入输出文件名 |

- 浮点容差那行按下去是要你输入一个数（默认给 `1e-6`），不是开关；输入不是数字就保持原值并告诉你。
- `companion_auto_listen` 是面板上唯一**要下次启动监听才生效**的一项（端口同理，
  它只在启动监听时读一次，所以不在面板里）；面板那一行直接写了"下次启动生效"。
- 面板写的就是设置文件里那几个键，两边看到的是同一份数据，不存在"面板改了、JSON 没变"。

## View 菜单就是功能地图

`View → Algorithm Competition Assistant` 从 v2.8.0 起按用途分成 7 个子菜单，
而且**命令面板里搜得到的每一条命令，菜单里都有对应项**——回归测试按集合等式盯着这一条，
新增命令只进面板不进菜单会直接报错了（以前有 6 条命令只有命令面板能找到，
另有 8 条只绑了键、菜单里看不见）：

| 子菜单 | 里面有什么 |
| --- | --- |
| `Run` | 运行、清空后运行、跑完全部、只重跑失败、并行跑全部、强制重编、Benchmark、Debug/Release 切换 |
| `Test data` | 从文件导入、挑一个文件导入、导入同目录 `.in/.out`、剪贴板新增、导出、复制 输入/预期/实际、键盘选测试点、隐藏 phantom |
| `Stress test` | 开始、停止、带选项开始（挑文件 + 轮数 + 时限）、用上次反例的种子重放 |
| `Competitive Companion` | 一题一个新文件、送进当前文件、固定当前文件、停止监听 |
| `Problem` | 按 URL 抓题、题面转 Markdown、**本题评测方式（文件输入输出）**、模板片段、比赛计时器 |
| `Tools` | 环境自检 doctor、跳到第一个/最后一个错误位置、校准本机、练习统计、重置统计 |
| `Settings` | 上面那四个面板 + 语言切换（`Switch to English` / `切换为中文` 两条直达） |

菜单项的 caption 是静态 JSON，跟不了 `language` 设置（Sublime 的机制限制），所以菜单
一律英文；**状态栏、弹窗、面板内文案和命令面板里带 `description()` 的那几条会跟随语言设置**。
每条 caption 后面都括号写了快捷键，所以这张菜单同时是"哪些键被这个插件占了"的查询表；
要反向查命令名，`Ctrl+Shift+P` 搜 `Algorithm` 最直接（45 条全在里面）。

## 对拍 (Stress Test) 教程

对拍 = 用随机数据生成器不停测试你的程序和标准程序 (std)，一旦输出不一致就停下，把出错的输入和两边输出展示给你。适合排查 WA 的边界情况。

### 1. 准备三个文件（放在同一目录）

- **你的程序**：当前打开的文件，如 `main.cpp`
- **标准程序 std**：保证正确的写法（暴力 / 题解做法），默认 `std.cpp`
- **数据生成器 gen**：往 stdout 打印一组随机数据，默认 `gen.cpp`

`gen.cpp` 示例（随机生成两个 1~10 的数）：

```cpp
#include <bits/stdc++.h>
using namespace std;
int main() {
    srand(time(0) + rand());
    int a = rand() % 10 + 1, b = rand() % 10 + 1;
    printf("%d %d\n", a, b);
    return 0;
}
```

### 2. 启动对拍

在**你的程序**的编辑视图里任选一种：

- 菜单：`View` → `Algorithm Competition Assistant` → `Start stress test`
- 命令面板：`Algorithm Competition Assistant: Start stress test`
- 快捷键：`Ctrl+Alt+S`

**正解和生成器从哪来由 `stress_file_source` 决定，三种都写在设置注释里：**

- `pick_once`（默认）：这个 cpp 第一次启动对拍时问你（Windows 用系统「打开文件」对话框，
  **起始目录就是当前源文件所在目录**），选完记在该题测试数据旁边的 `<文件名>__tests.stress`
  里，同一文件以后再按 `Ctrl+Alt+S` 就直接开跑、不再问；换个题目文件重新各选一次。
  同目录已经有唯一一对 std/gen 时连第一次都省了。
  **配对只跟着文件走**：插件不再把你选的 std/gen 写回 `stress_std_file` /
  `stress_generator_file`，那两个设置只是"备选项"列在面板第一行，不会替你决定。
  （以前它会写回：于是给 A 题选过的 std/gen 变成了 B 题的默认答案，一按 `Ctrl+Alt+S`
  就拿别人家的正解开跑，屏幕上还毫无提示。）
  0 字节的 `std.cpp` 不算候选也不算"记过的配对"——空文件当正解，第一轮必然"发现反例"，
  而那个反例是编出来的。
- `same_dir`：只在源文件同目录按名字找（`std` / `brute` / `bf` / `sol` / `暴力` 与
  `gen` / `data` / `maker` / `rand` / `生成` 等，多个候选取最短名），**永不弹窗**；
  找不到就直接告诉你它查了哪个目录，然后才让你手动指。
- `auto`：旧行为 —— 同目录找 → 退回上次用的全局路径 → 仍不确定才问。

选定后轮数与时间限制用设置里的 `stress_max_rounds` / `stress_time_limit_seconds`。
对话框本身现在按原生分辨率渲染（PowerShell 进程先声明 Per-Monitor V2 DPI 感知），
高缩放屏上不再是拉伸过的那层虚影；这一步在老机器上会静默跳过，不影响选文件。
其它平台或 `stress_file_picker` 设成 `panel` 时用 Sublime 面板浏览。

**要问的时候会写清楚问的是哪一个**：正解和生成器分两次选，标题（以及 Sublime 列表的第一行、
状态栏、控制台）都是 `【第 1/2 步 · 选正解 std】慢但一定对的那个（暴力/bf/sol）`、
`【第 2/2 步 · 选生成器 gen】随机造数据的那个（gen/data/rand）`。
两次选到同一个文件会被拒绝 —— 拿程序跟自己比，每一轮都"一致"，那场对拍发现不了任何东西。

想每次都自己指定，用命令面板里的 `Algorithm Competition Assistant: Stress test with options (pick files, rounds)`，
它会依次问 std、生成器、每轮时限和最大轮数。

### 3. 查看结果

会打开 `xxx -stress` 输出窗口：

- 每轮通过滚动显示 `第 N 轮 ... OK`
- **发现不一致**时立即停止，展示：输入数据、你的输出、std 输出、逐行差异（`user:` vs `std:`）
- 跑满最大轮数全部一致则显示对拍通过；全部轮次超时会明确提示「未比较任何输出」
- 结束时一定会报告一行「对拍结束，用时 X 秒」，状态栏同步显示当前轮次，跑得慢和卡住能分清
- 中途可用 `Stop stress test`（`Ctrl+Alt+Shift+S`）停止，**立即生效**：正在跑的那个进程会被直接结束，不用等这一轮的时限走完。**这个快捷键只在对拍输出页生效**，切到哪个对拍页就停哪一个，不会影响其它操作
- **支持多个文件同时对拍**：每个文件有自己的 `xxx -stress` 输出页，同一文件重复开始会复用它自己的页面；关闭某个对拍页会自动停止它对应的那场对拍
- **多解题 / 浮点题也能对拍**：`run_settings` 里配了 `checker` 时由 checker 判定
  （不再逐行比对，输出任意合法方案都不会被误报），checker 的 `quitf` 消息会跟着反例
  一起显示；没配 checker 时 `float_tolerance` 同样生效，最后一位小数的差异不再算不一致

### 4. 注意事项

- 三个程序都从 stdin 读、往 stdout 写，调试信息请用 `cerr`（配合 `"ignore_stderr": true`）
- Python 文件也可以直接作为你的程序 / std / gen 参与对拍
- 对拍会使用 `run_settings` 里的编译命令；失败的反例可自动存为正式测试点
- **对拍的编译命令会自动去掉 `-DLOCAL` / `-DDEBUG`**：这两个宏会把本地评测用的调试输出
  （`#ifdef LOCAL` 里的 `cerr`、读文件等）带进对拍，白白拖慢每一轮；你自己的 Run/编译
  仍然用设置里完整的命令。插件自带的默认设置也不再包含 `-DLOCAL`
- 对拍的可执行文件编译在独立的临时目录里，**不会占用你源码目录下的 `.exe`**：
  对拍跑着的时候照样可以编译运行同一个文件
- 需要给整轮对拍设总时长上限时用 `stress_max_wall_seconds`（默认 0 表示不限）
- **生成器有独立的时间上限**：`stress_generator_time_limit_seconds`（默认 10 秒）。
  造数据通常比解题慢，用程序本身的时限（`stress_time_limit_seconds`）卡生成器会把
  「生成器超时」误判成「生成器失败」。生成器超时会跳过该轮，连续 3 次才停止并提示。
- **`-DLOCAL` + 循环里的 `debug()` 会让本地运行超时**：`std::cerr` 默认不缓冲，把整个
  数组丢给 `cerr` 的调试语句放在主循环里时，输出量是 O(n²)，本地 n=2000 就要 20 秒以上
  （实测同一份代码不加 `-DLOCAL` 只要 0.02 秒）。提交前记得删掉这类调试输出，或把编译
  命令里的 `-DLOCAL` 去掉（对拍已经自动去掉，普通运行仍然按你的设置来）。
- **文件名里有连续空格也没问题**：`P2517  ZJOI 2010, 基站选址.cpp` 这类名字会被原样传给
  编译器。以前「美化编译命令」时会把引号内的连续空格折叠成一个，导致 cc1plus 报
  `No such file or directory`（磁盘上明明有这个文件）

## 致谢

本插件基于以下开源项目：

- [FastOlympicCoding](https://github.com/Jatana/FastOlympicCoding) —— **基础框架（本插件由其二次开发而来）**
- [cph-ng](https://github.com/langningchen/cph-ng) —— verdict 颜色与详情面板设计灵感
- [FastOlympicCodingHook](https://github.com/DrSchwad/FastOlympicCodingHook) —— Competitive Companion 集成思路

## 设置示例

在 `Algorithm Competition Assistant.sublime-settings`（旧名 `cph-by-chenkx.sublime-settings`，Preferences > Package Settings > Algorithm Competition Assistant）中：

```json
{
	"language": "zh",
	"run_settings": [
		{
			"name": "C++",
			"extensions": ["cpp"],
			"compile_cmd": "g++ \"{source_file}\" -std=c++11 -o \"{file_name}\"",
			"run_cmd": "\"{source_file_dir}\\{file_name}.exe\" {args} -debug",
			"time_limit_ms": 2000,
			"memory_limit_mb": 256
		}
	],
	"tests_relative_dir": ""
}
```

> 想要 C++ 崩溃时给出**行号**，把 `compile_cmd` 改成：
> `g++ "{source_file}" -std=c++11 -g -fsanitize=undefined -fsanitize-trap=undefined -fno-omit-frame-pointer -o "{file_name}"`
> （`-fsanitize-trap=undefined` 不需要 sanitizer 运行库，所以 MSYS2 mingw 这类不带
> `libubsan` 的工具链也能用；换成 `-fsanitize=address` 需要 `libasan`，缺库时插件会
> 自动去掉 `-fsanitize` 重试一次并提示。也可以干脆用 `Ctrl+Alt+G` 切到 debug 模式，
> 由插件帮你加这组参数。）
