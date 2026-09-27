# MasterVault 合成语料实验报告

全部数据为固定种子合成信号或随机位流，不是真实母带；DSD/HiRes标签不证明音质。本报告只说明已测字节恢复与完整存储成本。理论定位与已有工作见 research.md。

环境：Python 3.13.9，Windows-11-10.0.26220-SP0；生成时间 2026-09-26T16:58:02.235663+00:00。

SQLite真实文件大小计入页、对象、索引、manifest和side-info。成熟对照通常使用完整whole-file WavPack封装ZIP；PCM整文件拒绝时先尝试无损--force-even-byte-depth，再以明确完整容器位宽的raw PCM编码并附原WAV封套，不使用pre-quantize。另测FLAC音频+原WAV封套ZIP。所有可比较对照均已恢复原文件并核对SHA-256。已有压缩流以ZIP_STORED包装，封套/JSON用deflate，计入ZIP目录。只要一个文件最终拒绝或改变原始字节，该组对照标为不可比较，绝不拿部分成功作分母。共享解码程序本体、安装依赖、源文件保留副本和恢复/构建临时空间不计入单包归档大小；需要部署时应另计这些成本。

| 语料组 | 原始 B | raw B | native B | semantic B | hybrid B | 选中 B | 选择 | ZIP B | 完整WavPack B | FLAC+封套 B | 恢复 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
| pcm24_96000 | 196,748 | 196,608 | 131,072 | 135,168 | 131,072 | 131,072 | native | 180,821 | 113,385 | 60,770 | 通过 |
| pcm24_192000 | 196,748 | 212,992 | 122,880 | 139,264 | 122,880 | 122,880 | native | 189,645 | 109,240 | 49,454 | 通过 |
| pcm_bitdepth_versions | 1,442,068 | 1,298,432 | 1,236,992 | 454,656 | 1,236,992 | 454,656 | semantic | 1,230,944 | 1,050,885 | 1,094,366 | 通过 |
| pcm_dirty_lsb | 1,048,712 | 1,007,616 | 974,848 | 983,040 | 974,848 | 974,848 | native | 940,923 | 832,018 | 857,600 | 通过 |
| pcm_dither | 786,520 | 835,584 | 843,776 | 851,968 | 843,776 | 835,584 | raw | 755,000 | 700,570 | 718,543 | 通过 |
| pcm_extremes | 14,480 | 49,152 | 49,152 | 49,152 | 49,152 | 49,152 | raw | 456 | 4,629 | 20,124 | 通过 |
| opaque_float | 1,834 | 49,152 | 49,152 | 49,152 | 49,152 | 49,152 | raw | 263 | 1,641 | not_applicable | 通过 |
| dsd64_container_versions | 6,291,826 | 6,635,520 | 2,248,704 | 2,260,992 | 6,635,520 | 2,248,704 | native | 6,294,111 | 6,299,743 | not_applicable | 通过 |
| dsd64_complement_versions | 4,194,526 | 4,440,064 | 4,435,968 | 2,256,896 | 4,440,064 | 2,256,896 | semantic | 4,196,079 | 4,199,914 | not_applicable | 通过 |
| dsd_byte_edit | 6,291,876 | 6,627,328 | 4,505,600 | 2,330,624 | 6,627,328 | 2,330,624 | semantic | 6,294,237 | 6,299,880 | not_applicable | 通过 |
| dsd_bit_shift | 4,194,526 | 4,440,064 | 4,427,776 | 4,435,968 | 4,440,064 | 4,427,776 | native | 4,196,065 | 4,199,896 | not_applicable | 通过 |
| dsd128_synth | 262,366 | 147,456 | 77,824 | 69,632 | 69,632 | 69,632 | semantic | 84,092 | 92,731 | not_applicable | 通过 |
| dsd256_synth | 262,366 | 143,360 | 90,112 | 77,824 | 77,824 | 77,824 | semantic | 79,999 | 92,397 | not_applicable | 通过 |
| dsd_tail_padding | 16,588 | 49,152 | 49,152 | 49,152 | 49,152 | 49,152 | raw | 513 | not_fully_byte_exact | not_applicable | 通过 |
| dsd_noise | 524,418 | 602,112 | 598,016 | 589,824 | 589,824 | 589,824 | semantic | 524,732 | 525,505 | not_applicable | 通过 |
| opaque_dst | 184 | 49,152 | 49,152 | 49,152 | 49,152 | 49,152 | raw | 313 | not_fully_byte_exact | not_applicable | 通过 |
| malformed | 8,414 | 49,152 | 49,152 | 49,152 | 49,152 | 49,152 | raw | 440 | not_fully_byte_exact | not_applicable | 通过 |

四个策略的完整大小由同一组源文件实测；auto只在这些已构造候选中择小，hybrid的局部新增成本贪心也不等于全局最优。无净收益包经过完整构建/验证后不保留在archives；所有原文件保留。因此执行实验本身不会释放源文件空间。

## 相对成熟对照和实际耗时

| 语料组 | 净省 B | 比WavPack省 B | 比FLAC封套省 B | pack s | verify+unpack+hash s | 保存状态 |
|---|---:|---:|---:|---:|---:|---|
| pcm24_96000 | 65,676 | -17,687 | -70,302 | 10.833 | 2.707 | created |
| pcm24_192000 | 73,868 | -13,640 | -73,426 | 13.981 | 5.468 | created |
| pcm_bitdepth_versions | 987,412 | 596,229 | 639,710 | 52.839 | 11.783 | created |
| pcm_dirty_lsb | 73,864 | -142,830 | -117,248 | 56.892 | 22.346 | created |
| pcm_dither | -49,064 | -135,014 | -117,041 | 46.683 | 0.116 | skipped_no_net_savings |
| pcm_extremes | -34,672 | -44,523 | -29,028 | 1.891 | 0.077 | skipped_no_net_savings |
| opaque_float | -47,318 | -47,511 | 不可比较 | 0.534 | 0.054 | skipped_no_net_savings |
| dsd64_container_versions | 4,043,122 | 4,051,039 | 不可比较 | 19.689 | 0.366 | created |
| dsd64_complement_versions | 1,937,630 | 1,943,018 | 不可比较 | 26.407 | 1.020 | created |
| dsd_byte_edit | 3,961,252 | 3,969,256 | 不可比较 | 29.481 | 1.445 | created |
| dsd_bit_shift | -233,250 | -227,880 | 不可比较 | 26.598 | 0.252 | skipped_no_net_savings |
| dsd128_synth | 192,734 | 23,099 | 不可比较 | 1.422 | 0.140 | created |
| dsd256_synth | 184,542 | 14,573 | 不可比较 | 1.778 | 0.142 | created |
| dsd_tail_padding | -32,564 | 不可比较 | 不可比较 | 0.955 | 0.089 | skipped_no_net_savings |
| dsd_noise | -65,406 | -64,319 | 不可比较 | 4.100 | 0.167 | skipped_no_net_savings |
| opaque_dst | -48,968 | 不可比较 | 不可比较 | 0.545 | 0.054 | skipped_no_net_savings |
| malformed | -40,738 | 不可比较 | 不可比较 | 0.573 | 0.083 | skipped_no_net_savings |

正数表示MasterVault更小，负数表示成熟对照更小。负结果全部列出。时长为当前机器单次墙钟时间，不是吞吐分布；构建包括选项枚举和自身校验，不能与纯编码时间直接排名。

## 反例与语料解释

- pcm_bitdepth_versions验证24bit、32bit左对齐及精确DC/极性/声道交换；pcm_dirty_lsb与pcm_dither保留全部脏低位和dither，不以validBits宣称可删除。
- dsd64_container_versions验证DSF两种位序与DFF同流；complement组验证整体反相和声道交换。这里的1MiB/声道高熵随机位流用于隔离共享收益，并非典型模拟DSD录音分布。
- dsd_byte_edit含原流、各声道前插17字节、以及前插后再complement+交换声道三份，区别普通CDC的编辑复用与变换不变量CDC的组合复用；dsd_bit_shift插1bit并丢末bit，检验byte定位的适用边界。结果不能外推成对任意bit编辑稳定。
- dsd128_synth与dsd256_synth是简单一阶sigma-delta调制器控制组，不是专业调制器或真实母带音质证据。
- dsd_tail_padding包含35个有效样本bit、非零无效尾位、不同非零末块padding与metadata。容器恢复必须保留这些非音频字节。
- opaque_float保留NaN payload、±0、无限和非规格化位；opaque_dst只是明确标名的不可解码合成DST结构控制；malformed为截断格式。它们只能原字节回退，不能声称支持DST编码。

## 对照失败详情

- dsd_tail_padding / wavpack: not_fully_byte_exact。
  - 35bits_lsb_nonzero_padding.dsf: encoder_rejected；warning: DSF file has partial-byte leftover samples!                                 blocks not padded with NULLs, MD5 will not match!                                 original md5: 94deea6befaf19b2e05f93a8bfa6b6b8                                 verified md5: c36f5714a279b2f72c7aa69f0bf8f0b1                                 MD5 signatures should match, but do not!
  - 35bits_msb_nonzero_padding.dsf: encoder_rejected；warning: DSF file has partial-byte leftover samples!                                 blocks not padded with NULLs, MD5 will not match!                                 original md5: 553511340797a44c46e2ac5d7c6a896c                                 verified md5: 0e1187f99cc8c5ef6889e82c1193149b                                 MD5 signatures should match, but do not!
- opaque_dst / wavpack: not_fully_byte_exact。
  - unsupported_synthetic_dst_not_recording.dff: encoder_rejected；DSDIFF files must be uncompressed, not "DST "!
- malformed / wavpack: not_fully_byte_exact。
  - truncated_dff.dff: encoder_rejected；C:\Users\diwen_yu\Documents\Codex\2026-09-26\wo\outputs\mastervault\experiment_results_final\corpus\malformed\truncated_dff.dff is not a valid .DFF file (by total size)!
  - truncated_dsf.dsf: encoder_rejected；C:\Users\diwen_yu\Documents\Codex\2026-09-26\wo\outputs\mastervault\experiment_results_final\corpus\malformed\truncated_dsf.dsf is not a valid .DSF file (by total size)!

## 执行版本与可复现性

本轮在主模块导入前与结束时记录并核对mastervault.py、formats.py、transforms.py、codecs_layer.py、fixtures.py、benchmark.py的SHA-256；六文件起止完全一致。README、启动器和验收脚本不属于该算法/实验冻结集合。source_sha256为结束值，source_sha256_at_start为开始值。

`results.json`保存逐文件输入SHA-256、四候选真实字节数、策略/编码器统计、原始工具命令和错误、独立恢复结果、源码hash与工具来源。未验证任何首创、无未知反例、普遍优于成熟编码器或真实母带质量结论。
