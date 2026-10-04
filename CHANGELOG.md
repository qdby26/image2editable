# 更新日志

项目版本变更记录。正式版本通过 Git tag（例如 `v0.3.0`）发布。

## [Unreleased]

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
