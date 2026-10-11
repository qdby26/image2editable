# 更新日志

项目版本变更记录。正式版本通过 Git tag（例如 `v0.3.0`）发布。

## [Unreleased]

- layout-first 元素一致性规则与 unit PNG 发射（修复"同种元素两种待遇"+ 数字碎裂）：`_suppress_mixed_unit_text` 重写为双路径——(a) 单文本单元部分识别：读出的文字项旁有同色同尺度未读字形（如 "70" 读出而 "%" 未读）→ 整单元转 PNG，避免半原生半图片；(b) 兄弟卡片系列：跨 ≥2 张同排兄弟卡出现在相同相对位置、尺寸相近、颜色家族相容（绝对差 ≤45 或同色相家族/同亮色）的元素槽位，只要任一成员为未读墨迹或将被 emblem 通道栅格化（亮色+紧凑+中性卡面），该槽位所有已读文字项统一抑制——宁可全系列都是图，不要"0 是文字、其余是图"；高亮卡数字深色重着色经同色系判定通过，栏目标题旁有图标不误伤（标题与图标色族不同）。被抑制槽位/未读行直接发 `unit_XXXX.png`（行链水平闭运算归组同槽位兄弟字形，"01" 一张图），数字墨迹不进残差通道故与下方图标永不接触；行链只并入纵向重叠 ≥62% 的同行组件，且拒绝面积 <6% 最大成员且颜色差 >60 的异色细条（橙装饰线不再混入 "03"）；unit 发射时强像素按成员 comp 掩码+3px 缝裁掉外来连通域（徽标竖弧成独立对象），软 halo 不动；同卡 specs 合并需纵向真实重叠，水平邻近不焊。同时修正原实现颜色比较的通道顺序错误（拿 R 通道对 B 值）。`_split_owned_bridges`：卡内残差轮廓内含 ≥2 个硬核墨迹且硬核间距 ≥页高 1.8% 时按最近硬核归属拆分——光晕/AA 弱桥不再把不连通对象焊成一块。emblem 去重：同簇 >70% 重叠不再重发（修复同 bbox 双徽章）。`_geometry` 内孔判别对 ≥1% 页面积且 counter 占比 <15% 的大容器豁免——深蓝大卡内白色内容（数字/显微镜/标题）不再被当字形内孔误拒。`_release_boxed_decorations`：OCR 整框文字掩码内与文字色强偏离的连通装饰组件解除掩码（"本课重点" 橙色 pill 不再被吞成文字）。`_group_layout`：unit visual 不再按子 region 切片成 `_frame_` 碎片。png2 p03（真 v03 导出页，数字+复杂图标+标题结构）实测 v23：4 卡全原生形状（含深蓝卡）、每列恰好"数字 unit + 图标 unit"两张 PNG 共 8 个、4 栏目标题全原生、橙色 pill 独立 PNG+原生白字、unit_0002 橙像素 0、残差对象 18（候选 10）、PowerPoint 16.0 真渲染与源图全局 diff>40 仅 1.71%。`test_layout_first` 87 passed。已知限制：对象归组为局部规则，复杂相贴装饰仍需人工复核（warnings 已声明）。
- png2 前5页 v8：卡片内图标残差的 alpha 由 `difference>8` 硬二值改为偏离度线性渐变（dev 8→48 升到不透明）——图标背后的软光晕/阴影/AA 边缘不再被切成硬边矩形，p02 三个图标 PNG 现带半透明边缘（回归 `test_in_card_icon_halo_extracts_with_soft_alpha`）。ILP render_qa：p01 0.9615 / p02 0.9073 / p03 0.9613 / p04 0.8515 / p05 0.8631。`test_layout_first` 79 passed。
- png2 前5页 v6/v7：p01 封面大标题"宫颈癌"还原为可编辑原生文字——`_recover_source_titles` 的墨迹门控原用绝对灰度 `gray<100`（假设深字浅底），对蓝底白字直接否决（calls=0）；改为相对裁剪边界中位数的偏差 `|gray-median(border)|>45`，亮字深底与深字浅底均可检（回归 `test_source_title_recovery_accepts_bright_title_on_dark_backdrop`）。标题恢复同时使幻影白条 card_0003（插画白色飘带碎片 [2139,786,2385,893]）落入文字掩码被自然拒检。`_clean_source_backdrop` 大块区域填充原本 ring 采样≥32 个浅色像素即采用浅色填充（本意浅底深字），对深底亮字把标题框填成浅矩形；改为浅色样本需占 ring 多数（≥50%）才采用（回归 `test_source_backdrop_cleanup_keeps_dark_surface_under_bright_title`）。`_geometry` 新增字形/墨迹判别：掩码覆盖率<55%（凹轮廓）或含"内孔"——被填充完全包围、颜色≈bbox内非掩码间隙色的偏离块（字形 counter）→ 拒绝原生形状走残差 PNG；内孔颜色判别区分了卡内异色装饰（黄框卡内黑字/黄灯泡、红卡内黑M）与透出页底的真内孔，避免误杀真实卡片（回归 `test_saturated_glyph_with_enclosed_counter_is_not_a_card`）。修复 p05 "70%+" 的 % 三段（上圈/斜杠/下圈）曾被当卡片画成实心蓝块，现为正确字形 PNG。产出 `output/layout-native-v7.pptx`。ILP render_qa：p01 0.9615 / p02 0.9065 / p03 0.9593 / p04 0.8494 / p05 0.8631，3/5 ≥0.90，无回归。`test_layout_first` 78 passed。
- 渲染回退新增 WPS 路径：真 PowerPoint 不可用时经 `KWPP.Application` 渲染（仅导出 PNG，不影响字体嵌入——WPS 的 SaveAs 静默忽略嵌入标志，嵌入仍强制真 PowerPoint）。`_start_wps` 先查 ROT：运行中的 WPS（用户日常会话）标记 borrowed 只借不杀（关文档不 Quit）；无则 `Dispatch` 让 SCM 拉起专属 `wpp.exe /Automation`，以新 wpp.exe pid 证所有权、用完 Quit，Quit 失败兜底 terminate，无法证所有权的实例一律当 borrowed 不退出。结果标 `renderer: wps`/`wps-owned`。根因记录：WPS 在 `HKCU\SOFTWARE\Classes\CLSID\{91493441-5A91-11CF-8700-00AA0060263B}\LocalServer32` 把 PowerPoint 自己的 CLSID 劫持到 `wpp.exe /Automation`（HKCU 优先于 HKLM），且 `.pptx` 用户关联 = `WPP.PPTX.6`。实测 `KWPP` 打开+Slides.Export 3840×2160 通过、spawned 进程自动清理；新增 4 项单测，`test_powerpoint_renderer` 16 passed、`test_layout_first` 75 passed。
- png2 前5页 v5：残差提取两侧统一小核 MORPH_CLOSE（12×11px），未被占用侧不再跳过归组——p03 分子球/显微镜等图标由约20个碎片合成完整单块、p04 医生照片由2块合成1块；close 后重新施加卡列左缘 5px 切断，修复 p02 插画经桥接并入整页轮廓（3840×1935）被 ≥85%宽×75%高 规则误丢弃导致整页左幅丢失（SSIM 0.78 回退）。新增 emblem 提取：亮色文字（lum≥200）+框内密集饱和墨迹+所在卡非饱和填充 → 判定印章/徽标，整块发不透明 PNG（红底白字一体），同时移除对应 text 项并清 residual 防碎片重发；饱和徽章卡（知识目标等）内白字按宿主填充色判别不误伤。`LF_DEBUG_RESIDUAL=1` 可打印每个残差轮廓的丢弃原因。产出 `output/layout-native-v5.pptx`（91.7s，PowerPoint COM 本轮可用，`renderer: powerpoint-owned` 真机渲染通过）。ILP render_qa 同套验收：p01 0.9593 / p02 0.9065 / p03 0.9531 / p04 0.8494 / p05 0.8615，3/5 ≥0.90（v4 为 2/5）；p04/p05 残余分差集中在照片边缘与字形栅格，结构无缺陷。`test_layout_first` 75 passed。
- png2 前5页 v4：修复用户反馈“page2又是一整张图”。`source` 模式原先把整页源图设成 `expected`，白卡与页底几乎同色、左侧插画贴边被背景吃掉，卡片残留在整页底层图里。现新增 `_dominant_backdrop`（缩略图量化取≥35%主导色）重建纯色页底为 `source_fill`/`background_rgb`，不再贴整页图；近白大卡用 `_gutter_split_card_masks` 按蓝色分隔行切出，残差按“未被占用/已占用”分开提取、接近整页的轮廓丢弃，并在卡列左缘切掉 1px 桥接避免左侧插画与卡片连成一整块。归组只允许不同填充色且宽度接近的相邻卡合并（标题条不再并入白卡）。复现 `test_source_backdrop_splits_white_cards_joined_by_page_gutter` 等先失败后通过，`test_layout_first` 75 passed。产出 `output/layout-native-png2-five-pages-v4.pptx`（SHA256 `32f7ef2d3952f1c5670698bab45cdc59cfa75c3389d98acb3f29392c8c790d0c`，复用OCR组装42.85s）：p02 底层改为白色填充、左侧插画独立成 1553×1935 PNG、3张大白卡+标题条共4个可独立移动组；p01 仍 `source_cleaned`（封面复杂底无法重建为填充色，标题在背景）。WPS 占用 COM 无真机渲染；对照图 `working/routeb/png2-five-native-v6/preview/`。状态 `five_page_preview_pending_review`，未做19页。
- png2 前5页抽样（已被 v4 取代）：新基准为 `working/png2` 的 v03 导出图（7679×4320），ASCII 副本缩到 3840×2160 后仅跑 layout-first native。组装修复厚卡片被 `_geometry` 标成无端点 `line`：厚区域改矩形/圆角矩形，真横线才带 `line_start`/`line_end`。复现 `test_wide_outlined_card_is_not_an_endpointless_native_line` 先失败后通过；相关 `test_layout_first`/`test_pptx_native_routes`/`test_shape_analysis` 共90 passed。产出 `output/layout-native-png2-five-pages-v1.pptx`（SHA256 `d22aa244d64230d1e0dd060434e457b0e99c1387094ad587629515e96c59a292`），复用OCR组装约50s。WPS占用COM，真PowerPoint渲染未完成；对照图在 `working/routeb/png2-five-native-v3/preview/`。p02卡片较完整，p01标题仍烤在背景，p03–p05图标/插图碎片多、OCR断句差。未扩展到19页。
- layout-first整套对象补修完成：嵌套卡片/框体保留父子组，跨框体图片按小容器优先分区且像素/alpha守恒，组装器按依赖创建子组；补浅色框检测、纵向渐变270度方向及跨多个框体公共横线独立归属。宫颈癌native/PNG两套19页complete-v10候选均嵌入字体portable=true并完成全页PowerPoint渲染；各82个独立移动渲染覆盖79卡片和3图片，77普通通过、5个p18目标排除邻近插图相交区域后通过，保留原始审计。p05/p13/p14/p15文字视觉改善已并入候选但不作为通用校准默认；p19尾字保持栅格，server重试不接生产。最新全量4318 passed / 25 skipped / 1 warning，相关346 passed，git diff --check通过。复用OCR重建native97.84s、PNG123.76s（v9完整重建口径，v10增量替换p17，不含新OCR及移动审计）。逐页证据位于宫颈癌项目working/routeb/complete-delivery-v10；候选仍有字形/浅色装饰等视觉限制，状态reviewed_candidate_with_disclosed_limitations。下述p05-only等记录为历史阶段。

- layout-first 卡片归组补修：完整包含的小卡片并入父卡片；框体参与归组，内部边线和文字随框体移动，相邻框体与脚注条保持独立。跨框体的 raster_component 按框体边界分区，像素和 alpha 守恒；框体仍保留原生形状。四项复现先失败后通过，292项相关回归及git diff --check通过。p05 native/PNG v3各五个卡片组移动审计，内部残留均0；字体portable=true、源候选哈希不变，正常/移动共四页PowerPoint渲染完成。只验证p05，跨区分割仍需其他页面回归，视觉pending_review，未并入19页稿。

- 补修小字号字形复测在旧FreeType下遗漏直立角度：已知48px问号复现128px模板估角2度，原±1度搜索无法测到0度；仅估角绝对值<=2度时额外复测0度，保留评分、字号范围和大字号路径，三份font_match同步。现有复现先失败后通过，Pillow10.4/FreeType2.13.2与Pillow12.3/FreeType2.14.3两环境各288项相关测试通过，git diff --check通过。p19隔离单字两轮独立/正反组批及直接模型正反序结果完全一致（么0.999389、？0.621213），仍未通过OCR门槛；后续四页真实重放文字数据完全一致，嵌入后正常渲染与旧稿逐像素一致，共12页PowerPoint正常/移动渲染完成、字体portable=true，三处目标移动残留0。p13宽框邻字误计已另存修正审计，视觉仍pending_review。

- 修复match_glyph固定128px模板对小字号字形的误低分：仅已选字体、估计字号<=128px且IoU<0.98时，在估计字号±2px/角度±1度受限复测，保留原评分；三份font_match同步。四项已知字体字号复现先失败后通过，封面大字号runs与原记录完全一致。高置信度成对中文引号噪声过滤同步三份检测器；layout-first缺左引号短片段可在同裁剪纯识别>=0.98、与原文本严格一致且四字匹配通过时恢复，重试合计不超过4次OCR。相关348项回归通过。p13主路径v3自动恢复`“点状”`为四个可编辑字符，字体嵌入及两页正常/移动稿全部PowerPoint渲染，4793个源白色笔画像素旧位置残留0；未校准局部IoU=0.644，视觉待审、未并入19页候选；p19仍未通过识别门槛。

- layout-first增加受限换行短尾字扫描：根据已有长行的字高及左边界查找下一行未覆盖笔画，拒绝空白、已覆盖文字、截断连通域及长横线，候选合用每页最多4次OCR，保留0.9接受门槛。新增复现先失败后通过，相关320项测试通过（3.84s）。p19真实转换已从0次进入1次局部OCR，但正常检测`么？`置信度0.7703未通过，保留源对象；p14/p15两页重放仍各恢复一处，p15四个ink runs保持一致，全部PowerPoint渲染。扫描修复不等于p19文字恢复完成。

- 修复亮字字形填充采样把抗锯齿边缘误判为颜色不稳定：仅从5×5腐蚀后的笔画内部采样，腐蚀使用零值边界；内部不足8像素时拒绝，保留颜色偏差15和字形匹配0.9门槛。新增复现覆盖边缘混色、真实填充变色和细笔画拒绝，相关315项测试通过。p13完整引号仍因中文字形匹配不足被拒绝，未调整二值化阈值或宣称完整恢复。

- 修复高置信度百分数后缀加号（如`70%+`）被噪声过滤误删：仅新增置信度>=0.98的标准数字/小数+`%+`接受分支，保留既有规则；三份OCR检测器同步。复现先失败后通过，相关314项回归通过。p05全新整页OCR真实转换恢复原生`70%+`（置信度0.9994），字体嵌入及正常/移动稿PowerPoint渲染通过，所测15721个源蓝色笔画像素旧位置残留0。字距仍与源图有差异，单页候选未并入19页text-v3；旧OCR缓存不自动更新。

- layout-first复杂源背景增加最多两次标题局部识别；样式无法恢复时保留源图并记录候选。深色背景亮字复用现有字体匹配器逐字还原，要求字形fit_iou>=0.9和稳定填充色，保留原字距；定位文字掩码按run边界生成，避免整候选框清理。宫颈癌封面单页已生成字体嵌入版并真PowerPoint渲染，标题移动后原亮笔画位置残留0；相关71项回归通过。两套19页final-v2已并入验证封面并完成38页真渲染及字体嵌入；内容页与v1渲染逐像素一致，不改变默认fast。

- 修复OCR有效字符占比过滤误删带标点短片段：置信度>=0.9的中文问句/感叹句，及置信度>=0.98的左括号+2–8个英文字母+逗号/顿号片段不再被标点比例误删。三份检测器一致；新增复现先失败后通过，相关295项测试通过。真实局部图恢复“吗？”和“(CT、”的检测坐标。layout-first已接入每页最多4次正文空档/行末回捞，含置信度、边界及重复覆盖检查；行末按连通域收紧，正文空档保留原范围，并修复NumPy坐标JSON序列化。相关299项测试通过，两页native-v7候选同时恢复“吗?”和“(CT、”，均完成PowerPoint16.0渲染；后续portable-v7完成字体嵌入（Arial替换为Arimo，Microsoft YaHei保留，not_embedded=[]、portable=true）及两页移动审计，嵌入稿和移动稿共4页全部真渲染。p14/p15所测1705/1385个源深色笔画像素位置残留均0，原稿哈希不变；此检查不证明字形、标点或背景逐像素一致，候选仍pending_review；后续两版主路径v16恢复p14/p15并通过字体及移动审计，已回填两套19页text-v3候选，38页全部PowerPoint渲染，其余各17页与final-v2逐像素一致。短ASCII+中文标点片段在亮背景、连通域逐字符对应及字形匹配通过时自动生成ink runs；否则保留原OCR结果。修复positioned text组未登记layout_object_id导致卡片归组KeyError，主组及子框ID分别保留且唯一，两份组装器同步；新增复现先失败后通过，相关272项测试通过。独立基线校准提升实验局部IoU至0.880，但未接入生产；p14半角问号及p15基线偏差仍保留待审。

- layout-first文本使用固定框，修复PowerPoint分组首次渲染长正文变竖列及备注错位；源背景稀疏墨迹局部清理保留渐变，避免整OCR框产生矩形接缝。新增复现测试并同步PPT组装镜像。19页native/png候选均完成真Microsoft PowerPoint渲染与字体嵌入，艺术字漏识别、复杂对象归组仍有限制，不提升为默认fast。

- 新增显式 `convert --layout-first --card-mode native|png --slide-size 16:9|original` 入口：复用现有 OCR、OpenCV 几何分析和 PPTX 组装，先识别完整简单卡体，再输出原生形状或完整卡片 PNG；标题条与卡体相邻时合并对象，文字保持原生，并按卡片组织可整体移动的组。不调用整页 SAM/LaMa 或旧五轮修复，不改 strict/fast 默认路径；纯色背景直接重建，复杂背景要求显式 clean background。支持源图哈希绑定的 OCR 复用，报告独立记录真实原生形状、卡片 PNG、图片对象、文本框与耗时，不以 assembled 冒充视觉验收。
- 原生组装支持测得的圆角比例、旋转、线性渐变、虚线及对象组，移除默认主题产生的额外阴影；PPT 脚本镜像同步。修复新对象路径中的浅色卡体漏检、描边/填充重复候选、文字掩码切断导航条、透明文字洞及照片白色边界像素丢失，均有先失败后通过的复现测试。owned PowerPoint 渲染改为在最后一份 presentation 仍存活时 Quit，避免 Close 先撤销 COM Application；真实 Microsoft PowerPoint 16.0 导出和清理已成功，不代表字体嵌入已验收。

- 补强字体 worker 本体的清理身份：监督器在启动进程仍活着时捕获真实 worker 的父子关系、创建时间与 exe，终止前再次核验；不再在 shim 已被杀后查询它的 exe，不再以同 exe 或近期启动推定所有权。身份未知/改变时跳过并记录警告，protected PID 不清理。新增 7 项模拟身份回归，timeout 文件 53 项通过，两个生产镜像同步。预先确认 foreign ROT 占用时快速失败，不再创建无用实例后空等；本机实测 0.11s、0 个新进程。
- hybrid editable 交付新增可选 `quality_audit`：保留最终视觉状态与非阻塞质量告警，将哈希绑定的质量证据留在 `pages/<page>/delivery-quality.json`，恢复时重新验证；`validated`/`fully_editable` 仍描述装配与编辑交付，不暗示所有视觉门通过。partial 页另将匹配最终 quality graph SHA 的组件图保存在 durable route-c 目录，支持后续组件回填，不改变旧请求 schema。
- 性能记录补初始化、逐修复相位、最终装配 span；汇总兼容实际 worker 发出的 `worker_finish`（此前只汇总 `worker`，造成运行时 worker 耗时空缺）。嵌套 span 是包含式时间，不可直接相加。相位与汇总相关 35 项测试通过。
- 通用 PowerPoint renderer 的发现路径不再注册表 `DispatchEx` 激活，而复用已核验的 owned POWERPNT.EXE /safe + ROT 启动，报告带 PID/应用路径，清理仅限已记录身份；保留测试注入 factory 的接口。同时禁用宏，并在导出期间出现额外用户文稿时保护进程、不 Quit。真实 COM 渲染/释放异常验证仍受本机 Office 激活阻塞，不声明异常已解决。
- 外部阴影认领在最初 far 围栏内最多四次重算以适应掩码增长引起的局部基线变化；认领区背景修复另排除临时 donor halo，防止柔影回灌，仅写回认领区、区外逐像素不变。c5 audit-v3 外缘508px→v4 136px，但内部重复342px；donor 修复后342→4，shadow ratio 0.00454低于原阈值。audit-v5 第2轮 editable/validated、visual audit pass、0 unresolved，233.1s（v3 414.3s/v4 347.8s）；裁剪后恢复验证通过，真渲染/字体仍待核验。
- Route C 新增 `resolve_partial_component` / `--component-id` + `--graph`，复用已有单图片 donor 替换：绑定 partial draft 哈希及 quality graph SHA，拒绝冻结/非失败/旋转/翻转/裁剪目标与非等尺寸 donor；只替换降级层，验证其他形状 XML、相对层序及其他部件不变，请求仍 awaiting_host。无自动付费调用；c5 真工件同像素 donor 的单层替换结构证明已通过（43个旧部件保留），不是生图/视觉修复证明，真实渲染验收待补。
- 本轮冻结代码回归：4179 passed / 25 skipped / 3 deselected（真 Office 集成），无其余失败。c3 audit-v3 仍 partial 61+6（2042.8s），c6 audit-v5 仍 partial 4+4（514.1s），残留违规未掩盖；试行 source-derived ink underlay 判据在 c10 真工件产生新违规，已撤回，保留原判据。

- `failure_policy="hybrid"` 的保底页升级为**组件级软降级**：质量修复耗尽的页若仍持有绑定的 fallback 资产（组件图、呈现层、重建背景），交付为部分可编辑页——冻结组件保留可编辑图层＋原生文字，失败组件经 fallback 图的保留 parent 层原样放回并在报告中标记 degraded；资产缺失或组件内容已整体丢弃时才退回整页压平。原生文字仅发射质量门冻结的文本节点，烤进降级层的文字不再叠印。降级页仍生成 Route A 交接请求，报告新增 `editable_component_ids`/`degraded_component_ids` 字段。
- 新增 Route C 页级回填接口 `image2editable.route_c_resolve`：校验 `awaiting_host` 交接请求及其绑定的源图／质量报告哈希，按输入页序定位目标页并自动分流——恰一张整页图的保底页原地替换该图片，partial 等其余页经 `replace_slide_content` 整页替换为已验收的 Route A 单页 donor（仅原生形状／显式样式／本地图片关系，拒绝图表、组、外链、嵌入字体；渐变色仅当全部停点为显式 srgbClr 时放行），验证其余部件逐字节保留并输出 resolution 审计 JSON，不改动原稿、不覆盖已有产物、不自动调用生图。另支持 `--background` 对 partial 页单独替换背景图（新增 media 部件并重指关系，slide XML 与其余部件逐字节不变），请求仍保持 `awaiting_host`。
- 组件修复请求新增 `plan_history` 字段：每轮请求回采前几轮已记录的方案（宿主方案与 fast 模式确定性方案均纳入；含动作＋对象＋执行后失败/冻结清单），让修复方看到已尝试动作及其结果，避免在相同证据上重复相同归一化方案而触发 `repeated_plan` 停机。
- Fast 模式确定性修复计划增加轮次升级：第 3 轮起按上一轮质量报告的违规类型定向选择几何动作——`duplicate_shadow`/`missing_edge`（违规像素在掩码外、本属该组件）改发 `expand` 认领边缘带，`duplicate_pixels`/`alpha_halo`（掩码内重复残边）改发 `shrink` 侵蚀；margin 由 `edge_width_px + 容差` 逐轮递增折算（封顶 5%），侵蚀后会掏空掩码的组件保持 `accept` 诚实降级；无逐组件报告时回退盲 `shrink` 0.005 保底死锁破解。residual owner 继续走 `accept`+`absorb_residual`，文本节点不受影响，`rebuild_background` 照常回收释放像素。
- partial 交付的降级层改用**源图真值像素**重绘：降级层不再声称编辑语义，其 RGB 通道在 alpha 内直接取源图像素替换提取残留（实测约 1.1% 的伪像素会被抹掉），形状/边界保持不变；冻结层不受影响。
- 修复页级推进循环的耐久边界过紧：上限按 `advance` 调用次数封顶（`MAX_REPAIR_ROUNDS*6+4`），而走完 5 轮修复再叠加 fallback 尾链（`fallback_required→executed→quality_recorded→终态提交`）可超额崩溃。上限放宽至 `MAX_REPAIR_ROUNDS*12+8`，并新增真·无进展检测——连续两次 `processing` 间 `(phase, revision)` 不变才判死锁，崩溃语义从"调用数超限"精确为"无持久进展"。失败 run 可经 `runtime.retry_page` + `run_job` 从 durable 相位续跑（c5 实测：停在 `fallback_quality_recorded` 的页重试后 2 秒走完全程交付 partial）。
- 修复 `_visual_metrics` 提前返回字典缺 `texture_deficit` 键导致 `KeyError`：当洞没有 donor 可见边界像素（组件贴画布边缘或被其他组件完全包围）时，`empty` 返回缺少该键而消费端无条件索引。三处镜像同步补齐。
- Parent fallback 支持**外部阴影认领**：`component_quality.claim_exterior_shadow_pixels` 以质量门同一套判定（未归属、暗于基线、背景与重建均未改动、连通且受限、邻接唯一归属）圈出组件掩码外的投影带；恢复父层掩码时把认领带并入并同步从重建背景抹除。认领作为显式不交集扩展写入 `record_parent_fallback_execution` 审计——校验 `grown == initial ∪ claim` 且 `claim ∩ initial = ∅`，拒绝无认领记录的扩掩码与相互重叠的认领；认领引用持久化在 `state["fallback"]["shadow_claims"]` 并随 `parent_preserved`/`warning` 终态保留。inactive（已弃用）节点的旧掩码仅参与 containment、不再制造邻接歧义。校准改用与质量门一致的页面文本掩码（`_text_cleanup_mask_path`）。真实验证：c5 海报页降级层 alpha 净增 2224px（全部位于卡片右/下缘影带），背景对应区已重绘；页因其余违规仍 `preserved_with_warning`，认领掩码随降级层照常交付。
- partial 交付的原生文本发射加置信度门：冻结文本 `confidence < 0.6` 或单字符 ASCII 且 `confidence < 0.9`（含缺失/非法值）时不发射原生框、亦不挖除——保留烘焙像素保真，杜绝 OCR 误检字符直接上稿；被抑制的文本继续以降级层像素呈现。另加**孤立单字符抑制**：单字符 ASCII 即使置信度达标，若距其他所有文本中心距离 ≥ 4×自身盒边长也视为误检（实测 c3 的 "m" 置信度 0.957、孤立 180px——置信度门放不过、上下文门才拦得住；KPI 数字 "7"/"0" 邻文本 40-66px 正常放行）。
- 六页语料复测（c3/c4/c6 带认领+发射门重跑）：**c4 由 partial 转为全可编辑 validated**——parent_0005/0006 的阴影认领清掉 `duplicate_shadow`，`parent_preserved` 过门；c3 仍 partial（61+6）但 4 个降级 parent 全部记录认领；c6 仍 partial（4+4）1 处认领。三页认领均持久化在 `state["fallback"]["shadow_claims"]`。
- 修复 `discard`/`absorb_into_parent` 等使节点转为非活跃的动作导致 `inactive source provenance changed` 拒收：输出图序列化原先把所有非 frozen 节点的掩码重写为 `masks/{id}.png` 并重算 sha/bbox，而初始证据掩码带编号命名（`masks/NNNN-{id}.png`，frozen 节点跨轮保留），重写使非活跃节点字段偏离来源记录，被 `_validate_inactive_source_provenance` 正确拦截（c14 实测崩溃）。序列化改为：`state=="inactive"` 且掩码数组与原始载荷逐像素一致的节点按 frozen 同款保留原路径/原字节/原哈希；merge/split 当轮新建节点（无源载荷）或像素真被改动的节点回落重写分支，校验照常观测。三镜像同步。
- 边界残屑宽恕规则补充两条邻接分支：`material_ownership_metrics` 原先要求未认领连通区面积 ≤ `boundary_area_limit` 才判边界残屑，导致父层掩码差 1-4px 时产生的贴边残屑判 `unexplained_visual_residual`（实测 c17：283px×1px 抗锯齿细线；c10：848px 卡描边环，厚度 4px 超局部阈值）。现改为两条路径——细长区（厚度 ≤ 边界阈值）≥60% 像素落在已认领邻域内即宽恕；或任意长度区（厚度 ≤4px）≥90% 邻接即宽恕。真·遗漏元素（连接器/小图形只端点接触或无邻接）照常上报。c17-v3 端到端实测：该违规从未解清单消失，其余真实违规不受影响。三镜像同步。
- 14 页扩展语料 c7–c20 批跑：**12/14 全可编辑 validated，2 partial，0 未交付**；阴影认领再录 8 处（c7×1、c10×2、c17×5）。partial 页未解违规为 underlay 接缝族（c10）与 `duplicate_shadow` 残留/`background_text_residual`（c17）等非认领类别；三个孤立单字符对抗页（大数字/代码行号/独立页码）全部 editable，孤立抑制规则未在生产路径触发。
- c10/c17 partial 页遗留违规清零（四项修复，均先写复现测试、真实 pf 工件回放验证）：① `native_text_underlay`——非归属文本被从组件掩码挖除后形成封闭字形洞，`blocked=dilate(text)` 规则跳过 hole-fill 导致 alpha 在字形处镂空（c10 标题 22825 洞像素/87%）；`build_presentation_layer` 现把**封闭洞 ∩ 实际文本像素**以 text_clean 封进 `generated`（仅排除 `other_ownership` 本掩码，逐像素落在 `dilate(text,≥1)` 允许区内、不触发 `underlay_out_of_bounds`）。**不排除更高层**：叠卡布局中同位置字形洞在各层都镂空、每层让渡给更高层会级联全漏（c10 "灌药"卡 464px 实录）——underlay 的本职就是在高层 alpha 镂空处垫底色。② `underlay_seam`——`boundary_color_mae`/`gradient_jump_p95` 加最小证据量下限：边界采样 <8 像素时报 0（c10 实测"违规"全部证据仅 2 个边界像素，MAE=9.5 系统计失真而非接缝缺陷）。③ `background_text_residual`——`_flatten_text_ghosts` 收尾复检：TELEA inpaint 在 dense CJK 笔画上留下的亚可见鬼影此前无兜底检测直接进入 `background-original.png` 并逐轮复用，现按连通文本区域升级整体重填（覆盖率 ≥0.5% 且 ≥64px）杜绝未检出 halo 当 donor 回灌；同时 `_residual_text_ink_mask` 残墨阈值下限提至 `max(6, noise*2)`（真实鬼影与残留笔画 delta≫6，卡缘微闪 ≤5——c17 回放残墨归零；曾试行的"越界连通域排除"实测会误杀条纹纹理上依赖稠密 speck 的 donor 保护机制，已撤除）。④ `duplicate_shadow`——`exterior_shadow_claim` 的 `darker` 用 -6 下种子但质量门按 `>4` 判影，外缘 1-2px AA 毛边永远 flaggable 却不可认领（c17 parent_0006 左缘 250px）；认领后追加迭代吸收——邻接已认领区、背景未改动、暗于基线 4 级、非他属且限于 `far` 环——真实工件回放残余 250→9px（比率 0.0004 ≪ 0.01 阈）。三镜像同步。
- 修复字体嵌入监管的 owned 子进程清理在 launcher 风格解释器下整体失效：`sys.executable` 为 shim（如 uv/redirector venv 的 `python.exe` 重执行到真实解释器）时 `Popen.pid` 是 shim pid，而 owned 记录的 `parent_pid` 指向真实 worker pid——`_kill_owned_process` 的父一致性校验恒假、超时/崩溃/畸形状态等 6 条清理路径全部漏杀（`tests/test_font_embed_timeout.py` 确定性红）。`_WorkerStatus` 现自报 `worker_pid`（状态与 result `worker_state` 均携带），`_cleanup_worker` 优先用真实 pid 校验 owned；worker 本体的初版同 exe/存活窗口兜底后来证实不足，当前补强为监督器提前捕获父子关系、创建时间与 exe，并在杀 shim 前清理身份核验通过的真实解释器；未知身份不杀。
- `image-to-psd` 镜像补齐 `image_to_ppt.py` 的 OCR 磁盘缓存特性（`_detect_text_cached`，上次同步遗漏导致镜像比对失败）；`tests/test_text_style_refinement.py` 改用 `np.ptp` 兼容 NumPy 2.0；paddlex 依赖的 6 个 OCR 词几何测试按导入可用性 `skipif`，系统环境跳过、项目 venv 全过。
- 基准语料统一至 `benchmarks/corpus/` 与 `benchmarks/release/`；Skill 通过部分克隆和文件清单仅获取运行所需源码，跳过基准、测试、演示图片和开发发布工具。

- 仅安装转换 Skill 时也自动准备项目 Runtime、依赖、OCR 和模型；Windows 新安装优先 D 盘及其他非 C 本地磁盘，macOS/Linux 优先其他已挂载本地磁盘，统一下载缓存和临时目录并复用已有环境。
- 项目 Runtime 使用当前仓库或 GitHub main 的本次提交，逐文件核对安装内容和导入路径，不以相同版本号复用旧代码，不从滞后的 PyPI 项目包回退。
- 发布说明从本文件提取对应版本，移除单独的版本说明文件；发行依赖直接维护在 `pyproject.toml`，安全政策移至 `.github/SECURITY.md`。
- 简化中英文使用说明，统一使用 Agent 称呼，明确组件修复周期最多 5 批计划以及图片目录不递归的限制。

## [0.3.0]

### 新增与变更

- 引入按页面内容信号进行路由的快速转换流程，减少不必要的 SAM、LaMa 和重复 OCR 工作。
- 增加 OCR 重叠去重、词级几何保留、字符级可编辑文字运行，以及艺术字字体、旋转、填充、描边和渐变识别。
- 增强背景恢复、视觉组件所有权、旧缓存 manifest 校验和缓存复用，避免重复计算与无效产物。
- 修复文字清理边缘残色、可变字体粗细丢失，以及漏字恢复后重复分割的问题。纯色分叉连接线可依据正负提示点在本地分离，保留原图像素。
- 改进重叠复合图层中的文字衬底归属，避免独立表格行或卡片冻结后因移除复合图层而出现文字下方空洞。
- 字体匹配排除 OCR 框边缘的表格分隔线，并在必要时按实际像素字号重测字形，避免小字被错误放大。
- 保留穿过文字区域的独立细线，避免 OCR 清理误删结构线；完成转换后清理中间图像并保留最终质量记录。
- 质量门禁失败后自动执行针对性修复并重新验证，检测无进展循环；只有通过验收的 PPTX 才作为最终结果交付。
- 修复当前执行中新产生的可恢复修复暂停状态直接进入装配的问题；复用冻结组件继续下一轮修复，保留重复方案检查及循环保护。
- 同步 `image-to-ppt` 与 `image-to-psd` skills 的生产脚本和运行约束。

### 质量与性能

- 所有识别文字以可编辑文本对象装配，艺术字保留可编辑字符及其样式信息。
- 严格质量门禁继续检查 manifest、warning、fallback、未解释像素和组件质量，不通过时自动修复，不降低门槛。
- 复用有效 OCR、视觉分析和背景结果，减少 token、模型调用和整体转换时间。
- 持续计算的模型请求不再受默认五分钟总时限限制；发送或接收期间长时间没有 CPU／I/O 活动会触发停滞检测，显式超时和取消仍生效。

## [0.2.0]

### 发布范围

- v0.2 核心门禁是严格的 14 页 benchmark：8 个图片页、`pdf-rotated-page` 的完整 2 页、`pptx-mixed-screenshot-candidates` 的完整 4 页。
- 核心集合包含 10 个 case；每个 case 执行 3 次独立重复，共 30 次尝试和 42 个累计页面。
- 受保护的 GitHub-hosted Windows 门禁把 case 分成 5 组并行运行，再由独立步骤核对完整覆盖、运行环境和性能。只有最终聚合报告可以代表 benchmark 通过。
- 仓库中的其他生成语料用于补充覆盖，不计入 v0.2 核心成功率。

### 严格门禁

每页必须同时满足 manifest 约束、预期状态、0 warning、0 fallback、0 unexplained pixels 和 0 quality violations；任一重复失败都会使报告失败。核心 runner 使用已安装发行包和固定 plan 证据。diagnostic 只用于审核 GitHub-hosted 环境的 plan 绑定，不能生成正式通过报告。

### 运行与发布边界

- `image2editable --version` 从已安装 distribution metadata 读取版本；当前版本为 `0.2.0`。
- `image2editable doctor` 用于检查转换依赖和固定运行时模型；组件决策统一由 Host Agent 完成。
- 发行包契约矩阵覆盖 Windows、Linux、macOS 的 Python 3.10–3.12；真实性能比较只接受与 manifest、依赖约束和运行环境完全一致的基线。
- 运行时模型权重、模型缓存、临时 workspace 和生成的 PPTX 不进入 wheel、Git 或 benchmark 工件；Release Gate 只保存必要的 JSON 证据。
- PowerPoint 原生对象、截图候选和 OCR 文本的边界保持严格校验；不以 warning、fallback 或未解释像素换取通过。

### 安全与版本

该版本的安全政策定义 0.2.x 的私密漏洞报告流程：48 小时内确认、7 天内完成初步评估。发布 workflow 只响应 `v0.2.0` tag，验证同一 commit 的 release-gate 产物后创建 draft release，不自动发布。
