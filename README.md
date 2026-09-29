# MasterVault：HiRes PCM / DSD 逐字节无损归档 Demo

## GitHub 源码版说明

此仓库包含源码、测试、研究与论文核查记录、最终实验报告和带许可证的便携 WavPack。为了控制仓库体积，不上传个人音频、合成语料、生成的归档、恢复副本、Python 虚拟环境或 IDE 设置。后文关于完整分发 ZIP 的内容描述原离线分发包，不代表 GitHub 仓库包含这些生成文件。

在本目录用 Python 3.11+ 重新生成并验证完整流程：

```powershell
py -3 demo.py --output .\my-demo
py -3 -m unittest discover -s . -p 'test_*.py' -v
py -3 benchmark.py --output .\new-benchmark
py -3 large_file_check.py --output .\new-large-check.json --work-root .\large-scratch
py -3 release_check.py --results .\new-benchmark\results.json --large-file-report .\new-large-check.json --output .\new-verification.json
```

`release_check.py` 的默认输入针对完整离线分发包；从此源码仓库运行时，请先生成上述结果，再传入示例中的显式路径。已有 JSON 和测试日志是此前验证记录；本机新生成的记录才代表本机本次运行结果。

适合**同一母带的多个精确相关版本**：24/32 位整数表示、精确极性或偏移版本，以及 DSF/DFF 封装、DSF 位序、DSD 整体反相和声道交换。工具寻找可共享的内容，保留每个原文件的全部位、文件名和容器字节。打包时实际恢复并与源文件逐字节核对；日后解包则校验归档保存的文件SHA-256。

这是可运行的研究 Demo，不是新的音频编码标准。单份母带通常先用成熟 FLAC/WavPack 更合适；本工具也实测了自己明显落后的情况。输入端原生识别 WAV/RF64、DSF、未压缩 DFF；已压缩 FLAC/WavPack 输入仅作原字节归档。`.mva` 是存储归档，播放前需恢复原文件。

## 运行

Windows 上需要 Python 3.11+，无 pip 依赖。本次实测 Python 3.13.9。

在本目录打开 PowerShell：

```powershell
# 生成小型合成PCM/DSD示例，归档、恢复并独立逐字节比较
py -3 demo.py --output .\my-demo
# 或使用启动脚本，自动选一个新的结果目录
.\run_demo.ps1
```

演示默认仅用 Python 标准库，可离线运行。输出包含源文件、`example.mva`、恢复文件和 `demo_result.json`。示例是为了隔离共享效果生成的随机数据，不是真实母带录音。

归档自己的文件或目录：

```powershell
py -3 mastervault.py pack 'D:\Masters\Album' -o 'D:\Archives\Album.mva' --work-dir 'D:\Temp\MasterVault'
py -3 mastervault.py inspect 'D:\Archives\Album.mva'
py -3 mastervault.py verify 'D:\Archives\Album.mva' --work-dir 'D:\Temp\MasterVault'
py -3 mastervault.py unpack 'D:\Archives\Album.mva' -o 'D:\Restored\Album-new'
```

`pack` 接受多个文件或目录。整个目录输入会保留其顶层目录名及子目录；单文件输入保留文件名。`unpack` 的目标必须尚不存在。归档路径也必须是新文件，程序不会覆盖已有归档或删除源文件。

`pack` 构建候选、完整解码，并与当时的原文件逐字节比较后才发布。若完整归档大小不小于原文件字节总和，返回 `status: skipped_no_net_savings`，退出码为 0，但不生成归档。自动化应读取这个状态，不能仅以退出码判断已生成文件。`--allow-growth` 可显式保留膨胀包作实验。

## 格式与完整性

| 输入 | 音频内容识别与共享 | 原字节恢复 |
|---|---|---|
| WAV / RF64 整数 PCM，16/24/32 bit、1–8声道，含 WAVE extensible | 支持；按相同样本数分块 | 全部样本位、头、未知chunk、尾部 |
| 32 bit容器声明valid24，但低8位非零 | 仍保留实际32位；不按声明截断 | 包括全部脏低位 |
| DSF，LSB/MSB 位序，1–6声道、4096字节块 | 支持；统一位序后独立处理声道 | 位序、无效尾位、非零padding、metadata |
| 未压缩DSDIFF / DFF，1–8声道 | 支持；还原声道交错布局 | 全部容器字节 |
| DSD64 / 128 / 256 | 有相应合成语料实测 | 保持原DSD位流，不转PCM |
| float WAV、DST压缩DFF、FLAC/WavPack文件、未识别/格式不合规输入 | opaque原字节处理，不进行音频语义共享 | 原字节保存；不承诺额外压缩收益 |

HiRes PCM 测试包含 24 bit / 96 kHz、24 bit / 192 kHz 和 32 bit表示。解析器保留采样率字段，不把HiRes标签当音质证明。不会降采样、抖动、截位、重新调制DSD或把浮点NaN规范化。只改变存储表示。

音频识别要求单个非空音频数据块、受支持的格式版本，以及一致的声道、长度和布局字段。表中未列出的变体或结构校验不通过的文件会整体按原字节归档，不等于识别所有同扩展名文件。

保存范围是文件内容与相对文件名；文件系统时间戳、ACL、Windows备用数据流等不在归档范围内。拒绝符号链接、junction、冲突文件名和不安全路径。使用时源文件应保持静止。

## 方法与选择规则

1. **PCM精确表示共享**：先在精确整数域求相邻差分，再提取真实共有的2次幂因子与符号，保存每块anchor、shift、sign、帧数。24与32位使用相同样本数分块。整数模运算只用于最后编码/恢复，避免极值溢出破坏对应关系。
2. **DSD版本共享**：先拆声道并统一MSB位序，再用相邻字节XOR作为分块token及共享表示。整条声道施加相同XOR掩码不会改变这些token，整体反相是掩码255的特例；每块首字节和原容器布局用于恢复。
3. **实物归档择小**：构建 `raw`、`native`、`semantic`、`hybrid` 四个SQLite完整包，`auto`按真实文件长度择小。包含对象、恢复信息、页、索引和封套，避免只报payload去重率。`hybrid`按文件顺序贪心选择，**不保证全局最优**；实测也有它错过跨版本共享的情况。

`raw`直接对原字节分块；`native`解析容器并对独立声道分块；`semantic`增加上述可逆归一化；`hybrid`每个文件择一种配方。已识别的PCM可尝试FLAC对象、原生DSD可尝试WavPack对象；标准库raw/zlib/LZMA总能作为候选。任何外部编码器候选只有实际解码后字节完全相同才可使用。

`--mode auto|raw|native|semantic|hybrid` 可固定策略。`--average`接受16384至1048576间的2次幂，默认65536；DSD/raw表示CDC目标尺度，PCM表示32位等价预算，每块 `average/4` 个样本。实验报告使用16384，不能把其耗时当默认参数的耗时。

## 外部编码器

默认 `--codecs auto` 会尝试系统PATH中的FFmpeg和WavPack；本包附官方WavPack 5.9.0 Windows x64便携二进制与原许可。FFmpeg未随包附带，本次使用8.1.2。可设置 `FFMPEG_EXE`、`WAVPACK_EXE`、`WVUNPACK_EXE`。

- `--codecs stdlib` 生成只依赖Python标准库的归档。
- 用到FLAC对象的归档恢复时需要FFmpeg；用到`wavpack-dsd`的归档需要WvUnpack。`inspect`也会检查所需解码器。
- 32位PCM使用显式32位FLAC参数及逐字节回验，避免FFmpeg默认输出24位而静默损失低位。
- WvUnpack仅使用原生DSD `--raw`，不使用转换为PCM的选项。

工具二进制是复用的编码依赖/成熟对照，不是自研创新，也没有复制其源码。来源、版本、SHA-256和许可证见 `tools/provenance.json` 与 `tools/license.txt`。报告中的归档大小不含共享解码器程序体积或恢复临时空间；所有对照采用相同口径。

## 攻击、反例与允许使用的范围

可逆性有明确的逆变换，并在每块归一化、每个选中的编码对象、每次归档发布与恢复时验证。哈希仅用于索引；哈希相同时还比较真实内容，不能以碰撞替换不同数据。

| 针对性攻击 | 处理与结论 |
|---|---|
| 24位极值变成左对齐32位后共享失效 | 修复先取模再除移位的错误；精确整数差分后再编码 |
| 24/32位用相同字节数导致边界错位 | 改为相同样本数，并覆盖交替极值 |
| dirty-LSB、dither、削波、任意增益、signed wrap | 保留所有位；不承诺归一化后相同，失败可回退 |
| DSD整体反相、位序、交换声道、字节插入组合 | 独立测试完整恢复及共享；不从单变量成功外推组合 |
| DSD一位移位 | 已观察无法获得净收益；不声称对任意bit编辑不变 |
| 人为避开自然CDC边界，再前插1字节 | 已构造复用率降至零的反例；只保证正确恢复，禁止宣称普遍抗插入 |
| DSF无效尾位、非零padding、未知chunk、重复data | 保留非音频原字节；不支持的布局整体回退 |
| 强制哈希碰撞、坏checksum、越界长度、压缩炸弹、恶意SQLite元数据 | 内容比较、长度/内存预检、限额解压、schema白名单及错误拒绝 |
| 路径穿越、设备名、Unicode/case冲突、目标已存在、提交中失败 | 拒绝不安全路径；暂存验证后发布；不覆盖原文件 |
| 解码缓存混淆、错误shape、依赖撤除、淘汰/预算失效 | 键含完整压缩字节与codec/width/size；入口先验证；容量计账和真实进程次数测试 |

这是对已定义条件的验证，不是“所有可能反例已消灭”的证明。压缩收益失败与内容恢复失败分开处理：前者允许存在并如实报告；后者阻止归档发布。任何有限测试都不能证明没有未知缺陷。

## 验证与实验复现

分发ZIP包含源代码、攻击测试、论文核查、便携WavPack、最终合成语料、实际保存的归档、原始结果JSON与校验记录；另附无需外部编码器即可恢复的 `demo_example/example.mva`。为避免重复，不打包旧实验、恢复副本和成熟编码器的中间文件/对照ZIP；完整对照可用下列命令重新生成。`package_manifest.json`记录包内逐文件SHA-256，ZIP旁另附整体校验和。

```powershell
py -3 -m unittest discover -s . -p 'test_*.py' -v
py -3 benchmark.py --output .\new-benchmark
py -3 large_file_check.py --output .\new-large-check.json --work-root .\large-scratch
py -3 release_check.py --results .\new-benchmark\results.json --large-file-report .\new-large-check.json --output .\new-verification.json
```

完整实验包含PCM表示差异、dirty-LSB、dither、极值、DSD封装/反相/编辑、简单sigma-delta、独立噪声、尾位/padding，以及float、DST与畸形容器回退。每组均构建四候选并恢复校验。成熟对照包括完整whole-file WavPack包；WavPack不接受原PCM容器时，改用raw PCM编码并另存原封套。还有FLAC音频加原WAV封套包和ZIP。只比较能够还原全部原文件字节的对照，不把编码器拒绝当作胜利。

最终结果见 `experiment_results_final/report.md`、原始数据 `experiment_results_final/results.json`；测试数量和最终源代码/样本/归档SHA-256见 `verification.json`，详细测试日志为 `verification.tests.log`。报告保留净收益为负和输给成熟工具的结果。所有语料均为合成，未建立真实母带集合上的收益结论。

最终验收106项测试全部通过，0失败、0错误、0跳过；真实FFmpeg/WavPack测试均执行。9个保留归档又经过最终代码独立恢复及源文件核对。

冻结版本全量实验共17组、32文件，全部落盘恢复核对通过，保留9组有净收益归档，8组无净收益跳过。六个算法/实验源码的开始、结束SHA-256完全一致。全量实验墙钟时间355.60秒；不是受控吞吐基准。

| 代表性合成语料 | 原文件合计 B | MasterVault B | 与原文件相比 | 成熟对照 B |
|---|---:|---:|---|---:|
| 精确相关24/32位PCM版本 | 1,442,068 | 454,656 | 省68.47% | WavPack 1,050,885；FLAC 1,094,366 |
| 同一DSD流的DSF/MSB/DFF封装 | 6,291,826 | 2,248,704 | 省64.26% | WavPack 6,299,743 |
| DSD字节插入＋反相＋声道交换版本 | 6,291,876 | 2,330,624 | 省62.96% | WavPack 6,299,880 |
| 带脏低位的32位PCM | 1,048,712 | 974,848 | 省7.04%，但输给成熟对照 | WavPack 832,018；FLAC 857,600 |
| DSD一位移位版本 | 4,194,526 | 4,427,776 | 膨胀，默认拒存 | WavPack 4,199,896 |

WavPack有14组完整字节恢复可比较；DSF非零padding/部分尾字节、合成DST和截断容器3组最终拒绝，只标不可比较。FLAC有6组适用，全部完整字节恢复通过。单份24bit/192k合成PCM，本工具122,880 B，FLAC封套49,454 B，成熟编码器明显更小。

`release_check.py`默认使用上述最终基准与`large_file_check.json`，校验实验起止的六个算法/实验文件hash均等于当前实现，且大文件报告对应当前实现；旧版本或没有启动快照的报告会被拒绝。解压分发后按包内的`corpus`和`archives`定位文件，不依赖报告里原机器的绝对路径。重新测量时用`--results`和`--large-file-report`传入同次实现生成的报告。

`large_file_check.json` 是实际132 MiB单文件流式归档、恢复和内存测量，使用高度重复的合成PCM、`raw + stdlib`路径，只验证大文件通路及读取上限；极高压缩率不代表真实母带。另有RF64大于4 GiB稀疏文件的格式解析测试，不能等同于完整4 GiB归档验证。

本次大文件测量：输入138,412,076 B，峰值工作集77,758,464 B（74.16 MiB），最大单次源读取1 MiB；归档、独立校验、恢复和逐字节比较共40.20秒。该报告的五个实现/测量文件SHA-256也已绑定冻结代码。

流式对象与磁盘SQLite避免一次读取整首音频，但构建速度受Python循环、多候选和外部进程启动影响。解码LRU的8 MiB是编码体积＋解码体积＋估算开销的计账预算，不是进程RSS上限。SQLite也有独立8 MiB页缓存，解码器另占内存。

`verify`需要临时空间容纳最大的一个恢复文件；`unpack`需要所有原文件总大小。`pack auto`临时保留对象池、四候选及验证文件，峰值磁盘占用可能是输入数倍，可用 `--work-dir`放到空闲盘。发布使用同目录临时文件和硬链接，目标文件系统需支持硬链接；已在Windows/NTFS上验证。64 TiB、10000文件等是格式限额，**不是已经完成这些规模的性能验证**。

