# MasterVault 定向排重与证据边界

检索时间：2026-09-27。检索先于新语料/benchmark；只阅读规范、论文和官方工具文档，没有复制第三方实现。检索是约十分钟的定向检查，不是系统综述、专利排重或不存在性证明。没有找到精确同名方案不等于没有人研究过。

## 已有工作：不可作为本工具创新的部分

**DSD 无损压缩、DSF/DSDIFF 支持、原容器封套保存已成熟。** David Bryant 的 *WavPack 5 Porting Guide For Developers*（2016-11-20）§2 第 5 项已明确新增两种 1-bit DSD 无损压缩模式，并支持 Philips DSDIFF 和 Sony DSF；PDF第6页讨论 DSD，说明 API 以包含八个1-bit样本的字节为单位。该文还把 SACD 的 DST 作为既有压缩方法比较，不能把“给DSD做无损压缩”说成新方向。

- 官方原文：<https://www.wavpack.com/WavPack5PortingGuide.pdf>
- 官方产品页：<https://www.wavpack.com/>。当前5.9.0明说支持DSD、8/16/24/32位整数、32位float、DSF、DSDIFF和metadata/headers完整恢复。
- 官方下载页：<https://www.wavpack.com/downloads.html>
- 本demo随附的WavPack/WvUnpack是官方现成编码器对照/可选对象编码器，不是自研算法。

**对有效位数、冗余位和移位的利用不是空白。** David Bryant, *WavPack 4 & 5 Binary File / Block Format*（2020-04-12），PDF第3页定义了extended/shifted integers、decode后left-shift位数等格式字段。官方5.9.0手册的`--merge-blocks`说明编码器会分析真实有效位与冗余位；`--force-even-byte-depth`说明非零低位padding如何完整保留。因而共同2次幂因子或无效位处理本身不是新发现。

- 官方格式原文：<https://www.wavpack.com/WavPack5FileFormat.pdf>
- 5.9.0手册取自官方二进制ZIP中的`wavpack_doc.html`，未复制程序源码。
- 文档还明确`--pre-quantize`会丢弃位；本工具的字节级无损任务不得使用它。
- 实测边界：`--force-even-byte-depth`对32bit容器内声明valid24的脏LSB仍拒绝，因为24已经是完整字节数；该选项不会把valid24提升为valid32。不能据此断言WavPack压不了这些32bit样本。基准会再用显式32bit raw PCM输入、保留完整原WAV封套，独立恢复逐字节核对后才计入完整对照大小。

**共享主体+恢复信息属于Generalized Deduplication。** Rasmus Vestergaard, Qi Zhang, Daniel E. Lucani, *Generalized Deduplication: Bounds, Convergence, and Asymptotic Properties*, IEEE GLOBECOM 2019；同作者的*Lossless Compression of Time Series Data with Generalized Deduplication*, GLOBECOM 2019，均已有相关框架。GreedyGD进一步研究面向压缩数据直接分析的基位选择与配置加速；其预印本§4.1–4.2分别描述BaseTree计数与GreedySelect。交付前已用Crossref、OpenAlex与可取得的arXiv原文独立复核下列书目信息；据此定位方案，不把词汇重命名当创新。

- *Generalized Deduplication: Bounds, Convergence, and Asymptotic Properties*, GLOBECOM 2019, pp.1–6：<https://doi.org/10.1109/GLOBECOM38437.2019.9014012>。作者扩展全文为arXiv:1901.02720v4（2019-08-07）：<https://arxiv.org/html/1901.02720>；[arXiv书目页](https://arxiv.org/abs/1901.02720)直接列出该会议DOI，不把15页扩展稿说成6页会议排版稿。
- *Lossless Compression of Time Series Data with Generalized Deduplication*, GLOBECOM 2019, pp.1–6：<https://doi.org/10.1109/GLOBECOM38437.2019.9013957>。机构仓储链接：<https://pure.au.dk/ws/files/187638094/GLOBECOM2019.pdf>；最后一轮复核因站点证书过期未能重新取得该PDF，题名、作者次序、年份和页码由Crossref/OpenAlex确认，本轮不新增依赖其全文的具体论断。
- Aaron Hurst, Daniel E. Lucani, Qi Zhang, *GreedyGD: Enhanced Generalized Deduplication for Direct Analytics in IoT*, **IEEE Transactions on Industrial Informatics 20(4), 6954–6962, April 2024**：<https://doi.org/10.1109/TII.2024.3353913>。同题同作者的**2023年预印本**为arXiv:2304.07240v1（2023-04-14）：<https://arxiv.org/html/2304.07240>；[arXiv书目页](https://arxiv.org/abs/2304.07240)。作者确为Aaron Hurst等三人；预印本年份与正式发表年份分开记录，未声称两版全文完全一致。
- 最后一次书目审计的字段、来源URL、原响应SHA-256及访问限制见本目录`citation_audit.json`；完整API响应保留于开发工作目录，不随包重复分发。

**CDC对移位编辑及其性能权衡已有深厚研究。** LBFS（Muthitacharoen、Chen、Mazières，SOSP2001）PDF第4页讨论单字节前插破坏固定块，第5页§3.1.2还指出CDC的最小/最大块和周期输入也可破坏同步。FastCDC（Xia等，USENIX ATC2016）§2、§4.1讨论边界、CPU、去重率和索引成本。不能把CDC或修复前插字节失配作为创新。

- <https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf>
- <https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia>
- <https://www.usenix.org/system/files/conference/atc16/atc16-paper-xia.pdf>

## 格式规范与不得丢弃的内容

Sony *DSF File Format Specification*, Version1.01，PDF第3–6页定义chunk、bit-order和声道块布局。BitsPerSample=1与8对应不同位序，不能把DSF文件数据区直接当作DFF交错MSB流。DSF末块padding、最后一个字节中超出sampleCount的尾位和ID3/未知封套字节都属于“原文件逐字节恢复”的对象，即使不是有效音频也不能清零。

- 本轮已读Sony原规范镜像：<https://dsd-guide.com/sites/default/files/white-papers/DSFFileFormatSpec_E.pdf>
- DSDIFF v1.5原规范：<https://www.sonicstudio.com/pdf/dsd/DSDIFF_1.5_Spec.pdf>。并行格式代理已完整核验：§2.3定义大端chunk尺寸及偶数padding；§3.2.3第16页区分`DSD `与`DST `；§3.3第18页定义未压缩DSD每字节MSB优先、声道字节交错、每声道样本数为8的倍数；§3.4描述DST帧结构。本分支据代理原文核验结果引用，未声称随机DST封套是可解码的DST录音。
- Sony这份旧DSF规范的采样率表列DSD64/128；DSD256属于本工具明确支持并实测的扩展，不能把旧表说成列出了DSD256。
- DST压缩DFF与未压缩DSD是不同数据表示。当前不实现DST解码，只允许opaque原字节保存；没有将随机数据伪装成有效DST音频或把不支持改写为支持。
- float特殊值的NaN payload、负零、非规格化值不得经数值转换规范化。声明valid24的32bit PCM中的非零LSB必须作为原始样本位保存，不能凭字段丢弃。

## 本次待验证的具体差异：限定为GD组合实例

### A. 跨DSD封装与整体complement的精确共享

将DSF/DFF解析为独立声道、统一MSB位序的字节流；以相邻字节`d[n]=x[n] XOR x[n-1]`构造切块token。对于整条声道的固定XOR掩码`m`，有`(x[n] XOR m) XOR (x[n-1] XOR m)=d[n]`，complement是`m=255`的特例。因此在相同token位置、相同分块参数下，切点可保持这一不变量。保存每块首字节和必要的封套/尾位信息，解码必须逐字节恢复后才可共享。

这个代数事实不新颖，CDC也不新颖；本轮要测的是**结合跨DSF/DFF/位序包装、独立声道版本和byte-aligned编辑时，完整归档是否比成熟whole-file WavPack更省**。普通raw-PCM/raw-DSD字节CDC不自动具有上述变换不变量；这里只在适当token上定位。块最小/最大长度仍可能阻碍重同步，byte插入接缝会改变邻接token，1-bit移位与8-bit字节插入不是同一种变换。位移反例必须留在结果中。

### B. 跨24/32bit表示、精确2次幂缩放的PCM共享

使用signed样本的精确差分，提取真正共同的2次幂因子，保存anchor/shift/sign等恢复信息，并与32bit统一表示配合。只有差分实际可整除时才能提取，不依据validBits字段猜测。相同源24bit到32bit左移8位、精确offset/polarity的关系可以构造对应；真实dither、削波或任意非2次幂增益可能破坏该关系，必须保留全部残留位。

按原始字节数固定分块会使24bit与32bit的帧区间不同，破坏本来可共享的关系。实现需要按相同帧数（或统一32bit逻辑字节数）确定块边界。整数溢出/环绕语义与signed差分提取也必须经过极值反例验证。

差分预测、位移、符号归一化、冗余位分析和GD均已有先例。本轮有限检索没有核验到与A/B全部条件完全相同的音频归档论文，也没有证据证明不存在此类论文、产品或专利。不得声明“没人深入研究过”或首创。

## 检索记录与未建立的结论

本轮查询了DSD/DST lossless、complement invariant deduplication、transformation invariant chunking/deduplication、bit-depth invariant PCM deduplication，并直接读取WavPack官方规范、开发者指南和Sony规范。部分Crossref请求返回429，部分结果是语言学chunking等无关条目；这些均不构成“没有前人”的证据。DST算法的独立AES原论文和最新所有codec比较未在本时间窗内完成系统核验，不编造作者/年份/性能数字。

所有生成数据均为合成信号或确定性随机位流，不是真实母带；DSD采样率标签和有效容器并不证明模拟音质。强基线需保存全部原文件信息并执行独立解码后SHA-256核对；若成熟编码器拒绝非规范dirty-LSB/padding文件，要报告拒绝或使用显式无损封套，不能偷偷规范化后比较压缩率。SQLite文件页、索引、封套、side-info及可选编码器额外开销均计入完整包大小。

## 官方便携工具来源

官方5.9.0 Win64 ZIP：<https://github.com/dbry/WavPack/releases/download/5.9.0/wavpack-5.9.0-x64.zip>。

ZIP SHA-256：`ad5e94bcde6f4edfc859210d98f144f225805a391c633c68f6dbb15a9e52570e`。

`tools/wavpack.exe`、`tools/wvunpack.exe`仅从该二进制发行包提取，并随附原样`license.txt`及`provenance.json`。许可为BSD三条款，Copyright(c)1998–2025 David Bryant，允许满足许可条件的二进制再分发。没有下载或复制源码，没有系统安装。
