# Font Pool — 来源记录

内置字体池（OFL 1.1），双重用途：

1. **嵌入替换**：PowerPoint 拒绝嵌入 Windows 核心字体（Arial/Times New
   Roman/Calibri/Cambria/Courier New，fsType=0x8 但宿主策略跳过）。管线把
   这些 typeface 改写成度量兼容的可嵌克隆后再嵌入，保证换机不缺字。
2. **匹配池**：`font_match.installed_faces()` 额外扫描本目录，保证匹配
   候选在缺少这些字体的机器上仍确定可用。

| 目录 | 字体 | 度量兼容目标 | 形态 | 来源 |
|---|---|---|---|---|
| arimo | Arimo | Arial | VF `[wght]` + Italic VF | google/fonts `ofl/arimo` |
| tinos | Tinos | Times New Roman | 静态 4 式 | google/fonts `ofl/tinos` |
| carlito | Carlito | Calibri | 静态 4 式 | google/fonts `ofl/carlito` |
| caladea | Caladea | Cambria | 静态 4 式 | google/fonts `ofl/caladea` |
| cousine | Cousine | Courier New | 静态 4 式 | google/fonts `ofl/cousine` |

全部 SIL Open Font License 1.1（各目录附 OFL.txt），fsType=0（可安装、
可嵌入、可裁剪）。上游：https://github.com/google/fonts ，原始项目方
googlefonts/arimo、googlefonts/tinos 等。
