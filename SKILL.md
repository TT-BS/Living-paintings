---
name: living-picture
description: Make a still illustration or painting feel alive on a web page. Use when someone building a website or app wants a static picture (illustration, watercolour, oil painting, ink drawing, a photo of an artwork) to move a little, with phrases like "bring this picture to life", "animate this illustration", "living picture", "2.5D / parallax", "cinemagraph", "让画动起来", "让插画活起来", "动态插画". You read the picture yourself, decide what carries its spirit, split it into a few depth layers, model the parts that should move, and deliver a small dependency-free web player with the layers. You then check your own work by looking at rendered frames. Everything runs locally; no cloud service, no model download.
compatibility: Needs Python 3.9+ with numpy, scipy and Pillow for the picture tools, and Node 22+ with a local Chrome, Chromium or Edge for frame capture. The delivered player is plain JavaScript (WebGL) with no dependencies.
---

# 让画活起来（living picture）

让画中已有的世界继续发生。在实际显示尺寸下，主要动态可读，空间关系成立，画仍保有原来的笔触、构图与气质。每张图重新判断；示例、预设和数量都不是创作上限。

你负责看和判断；工具只做眼睛和判断做不到的事：沿你指的轮廓切出图层，用原画自己的颜料补上它挡住的地方，播放结果，再把帧交给你看。下面的规则是底线，不是天花板：看得出更好的做法（包括自己写代码）就去做，同样用画面检验。不要把一串参数或细小问题交给使用者，他们只需要说感觉。

本文件所在的文件夹就是这个技能的根目录；下面的命令都相对它来写。参考文档里有的是英文、有的是中文，内容同样有效。

## 规则

1. **先读画。** 保留原件，规范工作图的方向、色彩和透明度，记下分辨率。看全图与必要局部，用一句话记画意，再列材料、光与空气、支撑和遮挡、前后关系及保护区域。区分笔触画的是物体还是它的状态（拖痕、速度线、虚化尾迹表现的是运动）；透明媒介的观察色是颜料与底色的叠合。选最能传达画意的动态主导，其他响应围绕它组织。主导可以是一个动作，也可以是光、空气、水面这类遍布画面的变化。幅度随画意定，静的画以目标尺寸下可察觉为可读。因画意而小要写明依据，与缩幅遮错分开；有明确主体动作时仍按其要求，不能以“安静”遮错。

2. **写因果与节奏。** 说明原因、承载者、响应、后果与恢复。动作服从材料、重量、固定端和自由端，也可以来自生命体自身。对已采用的原因，沿作用范围、承载者及连接明确预期响应、保持静止的部分与依据；主次决定表现强弱，不能单独作为遗漏必要响应的理由。尊重原画正在发生的瞬间；持续风、流动或明确要求的循环不必强塞停顿。确认主导动态所需形态、支撑与全程遮挡有足够画面或建模依据；先预演关键状态，再投入精修和配角。依据不足时补足模型，或选择成立且可读的表达，不靠缩幅遮掩坏边。

3. **按关系分离。** 同一意象穿过不同颜色、明暗和背景仍是完整对象；各段可用不同提取方法，边界须连贯。原画有意虚化或融进底色的边缘保持虚化，不为分层造硬边；分不开的部分不强拆；已确定需要响应的交融区域可整体走流动或光变，并核实该表达仍承载所选画意与机制。此项只管原画本身就虚的边，清晰轮廓仍须完整提取。区分附着、支撑、接触、柔性连接与反射，再决定层。共同受力不等于同一刚体，提取为同一层也不等于只有整体位移；相对变形由材料和连接决定。选区须覆盖已选响应所需的完整结构，不能以颜色命中或局部框选代替完整性判断。部件继承承载者运动与空间，保留有依据的局部动作，并守住接触点。

4. **让动态与空间共存。** 一个播放器、同一时钟，先求自身动作，再组合承载者与空间变换。深度候选须核遮挡、落脚和轮廓；立在地面或台阶上的东西取它脚下那一处的深度，连续的面按行或按面渐变，不切成几张纸片。空间取自原画，记录动态与层间空间各如何可读，并在目标尺寸核实；不得为遮错默默取消已定空间表达，或以整图漂移代替主体动作。文字与装饰按所属参考系保护；画框结合画意判断。

5. **守住笔触与显露。** 根据媒介选刚体、弯曲、流动或光变，检查笔触宽度、方向、棱线及轮廓。局部变形只影响有依据的材料区域，并与同姿态下未施加该变形的画面比较，确认没有牵动邻近纹理。新释放的元素取自原画笔触，取的是物体本体与颜料本色：表现状态的笔触留在原位，透明媒介离开原底须恢复本色，与画中同类元素读作同一种东西；运动随材料，不为压住一个症状加约束而把它变成刚性平移。按部件、父体与镜头的联合范围补足背景，优先用原有图层，补底归属对应背景面，本层覆盖到的像素不作供体。软边按该层运动方式处理：小幅位移的层在软边下保留观察像素，整体离开原位的层连软边一起补；紧贴轮廓的描边属于对象。分别判断轮廓归属、软边覆盖、边缘颜色和背后内容；不能为合成重合而把补底误差塞进前景轮廓或原位置残影。检查完整移动轮廓、遮挡及叙事焦点。

6. **分清原图与播放状态。** 暂停、减少动态和失败回退显示无损工作原图；真正安静段须有可核实的休息与唤醒安排。连续微动不假报静止，第零合成帧不冒充原图。缺层、超限和不支持的组合必须明确处理。未具备的能力须实现、换可成立的表达或说明未完成。

7. **用画面验证。** 实看主导动态全过程及峰值（分布变化看变化最大处）、动态与视角联合极值、目标显示尺寸和连续播放。对照预期响应，核关键约束处、运动传递处及代表性可动区域，区分视差、随承载者运动与相对运动，并检查静止依据；整块位移不能证明局部机制成立。会离开原位的部件逐件放在深、浅及它将经过的实际底上验收，总览和帧条会掩盖单件缺陷。调大幅度或数量后，被放大的机制重新逐件验收。分别核原图回退、接触、显露、固定区、笔触、主次与节奏；帧条不能证明节奏，数值不能证明好看。用未参与调参的新结构检验迁移，保留失败和未审范围。

8. **节省重复，不省判断。** 资产处理与动态验收分别记依赖，复用未变材料，只重跑受影响环节。一轮没有改善可见结果时重新诊断，不无限微调或缩幅遮错。代理承担技术选择。可读动态、空间、笔触与关系确实不可兼得时，先换表达方式，不先削其中一项。补足模型和换表达都已试过仍不可兼得，有两条不让：接触、支撑、遮挡不破；不出现不取自原画的笔触。动态与空间之间，承载第 1 条那句画意的一项不让，另一项公开让步并记下让了什么。两项都承载画意，或让步后那句画意要改写，才交用户决定。工程上还没做到不算冲突，仍按第 6 条实现或说明未完成。静态回退能保住原画，但不能当作动态与空间已完成。

## 怎么做

边做边在图旁边记简短的工作笔记：看到了什么、定了什么、为什么。它让下一轮更快，也让你被问到时能一句话说清选择。图和由图切出来的一切属于使用者：工作目录放在会发布或进版本库的路径之外，除非对方说可以。

1. **准备和看图。** 一步开工：
   ```
   bin/living-picture new <原图> --job <job>
   ```
   它建工作目录、写 notes.md 骨架（原图路径与 sha256），自动去平台水印（见下），再 prepare 和 look。分步时：
   ```
   python3 scripts/picture.py prepare <原图> --out <job>/work
   python3 scripts/picture.py look <job>/work/picture.png --out <job>/look.jpg
   ```
   平台水印（小红书等，右下角的药丸标志和其下右对齐的 ID 文字）：`new` 会自动处理，`--keep-watermark` 关掉；分步时先 `python3 scripts/picture.py dewatermark <原图> --out <job>/clean.png`，再用 clean.png 去 prepare。两者都在才动手，画家签名不会被当成水印；在平坦的底上反推还原框下的画，在纹理密的底上用周围颜料重画，并补回颗粒。看它写出的对比图（原图｜修复后｜改动处），在显示尺寸和 4 倍下核对。找不到时给 `--pill x0 y0 x1 y1 --line x0 y0 x1 y1` 粗框。原件不改。
   在 Mac 上，`python3 scripts/picture.py vision <job>/work/picture.png --out <job>/work/vision` 用系统自带的 Vision 给出起点：主体与人物遮罩（可直接作 mask 选区）、脸与眼（现成的头部转动和眨眼条目）、肢体转轴、视觉焦点、文字与签名（固定层候选）。先看它的 `review.jpg`，每一条都核对后再用；见 `references/macos-vision.md`。
   `prepare` 把原图重新编码成 `work/picture.png`（去掉元数据、规范方向和色彩、保留透明度；iPhone 的 HEIC 和广色域照片也能直接读）；之后所有坐标都指这张图的像素。`look` 在全图或局部（`--box x0 y0 x1 y1`）上画带刻度的网格，定轮廓前先放大核对。量边界、眼睛和颜色用 `measure`（逐行区段、色块中心与现成的眨眼 mask、带余量的外轮廓）和 `palette`（点取色、区域主色），再用 `look --poly/--points` 画回去核对，不凭目测。

2. **读画、写动态脚本。** 按规则第 1、2 条，读 `references/reading.md`（读画的问题清单和关系描述库）。脚本写原因、主次、每个原因的传递链、静的时候还剩什么，以及访客的指针或触摸算“有人经过”。没有指针时（手机上）画也要活。只有当答案会改变脚本、又不能从画和它要放进的页面推断时，才问使用者一个关于整体感觉的问题；否则写下你的假设，继续。

3. **定层、切层。** 写 `<job>/plan.json`，运行 `python3 scripts/picture.py cut <job>/plan.json`。图层从前到后依次剥：剥下一层，用原画颜料补上它挡住的地方，再从这个结果剥下一层。看 `work/review/` 里的总览和逐层审查图，改到每一刀只切到那个东西、每处补底都像同一幅画为止，通常要两三轮。格式、选区方法和补底见 `references/cutting.md`，补底的约束见 `references/reveal-contract.md`。背景面在计划顶层用 `surfaces` 画一次，各层用 `"method": "surfaces"` 补底：`rim` 决定软边下保留原像素还是一起补，`as` 让开口周围续画内部；明暗利落的对象用 `level` 选区加 `contour`，水彩湿边用 `halo`，外部生成的选区用 `mask`，要离开原底的笔触用 `pigment`。看 `review/surfaces.jpg` 与 `review/rest-residual.png`，逐层用 `picture.py inspect`（释放元素加 `--pieces`）。

4. **建运动。** 写 `<job>/scene.json`：同样的图层，从后到前，各自的深度和动法。全画只有一个风、一个光、一个镜头，各层按自己的位置和材料响应；生命体的眨眼、偏头、耳朵在自身层内用通道驱动的形变完成；散落元素用 `tumble`、`windSpin` 控制翻转与自转；穿过明暗的东西给场景一张 `lightMap`（物体所在处的光，由画面读出）并对该层开 `relight`。词汇不够时用 `hooks` 写自己的代码。完整词汇见 `references/motion.md`；安静段的接口见 `references/runtime-rest-contract.md`。然后打包：
   ```
   python3 scripts/picture.py bundle <job>/scene.json --out <job>/out
   ```
   得到 `out/index.html`（双击就能打开的单文件）和 `out/site/`（放进网站的文件）。

5. **看、判断、修。**
   ```
   node scripts/capture.mjs <job>/out/index.html --out <job>/check --every 0.5 --duration 12
   python3 scripts/picture.py sheet <job>/check/f_t*.jpg --out <job>/check/sheet.jpg --motion
   node scripts/capture.mjs <job>/out/index.html --out <job>/check-view --pointer corners:1-9 --every 0.5 --duration 10
   node scripts/capture.mjs <job>/out/index.html --out <job>/video --video 20
   ```
   播放是确定的，同一秒总是同一帧。要近看的时刻先用 `capture.mjs … --every 0.1 --channels blink,tilt --track "layer:x,y"` 找出（通道峰值、位移最大处），再在那些时刻取高清帧。看事件全程的帧条、视角四个极端、每条会动的边和每张脸的放大（`sheet --box … --zoom …`）、一分钟左右的节奏图（`sheet --rhythm`），再看连续视频。`--reduced-motion --probe 3` 确认减少动态时不动。要看什么、每种失败长什么样，见 `references/checking.md`。修的时候修原因：坏边是切层或补底的问题，不是幅度的问题。宁可关掉一个动作，也不要交一个错的。

6. **交付。** 把 `out/site/` 放进项目，按 `references/embedding.md` 嵌入（容器里放原图作回退，一个脚本，一次调用）。播放器离屏自停、对要求减少动态的人显示原图、带暂停按钮，这些都要保留。告诉使用者：让什么活了、为什么，什么故意留着不动，哪里没做好；之后的修改用感觉的话来谈（更静、更活、更有景深、天气慢一点），由你翻译成参数。

## 交付前逐项看

| 底线 | 看什么 |
|---|---|
| 原图回退 | 暂停、减少动态、出错时显示的是无损工作原图；第零合成帧不拿来冒充原图。 |
| 显露正确 | 每个动作和视角的极值处：没有破洞、没有留在原位的旧轮廓、没有随前景带走的旧背景、没有糊斑。 |
| 立着的站住 | 视角移动时，没有东西在它脚下的面上滑。 |
| 一体一动 | 一个生命体或物体的部件不各自漂开；脸上不单独动一个五官。 |
| 看得见原因 | 一个东西动时，看得到推动它的原因也按顺序到达了它的邻居。 |
| 一眼看得出 | 在目标显示尺寸下，主导动态可读；安静的画可察觉，并写明为什么小。 |
| 耐看 | 没有一眼看穿的循环，没有快速闪烁，没有大片区域一直在动；持续风或流动是画意时例外。 |
| 文字和边稳 | 文字、签名、画框和画的边缘保持清晰，留在原处。 |
| 仍是这幅画 | 没有外来的东西；每一帧里媒介仍像它自己。 |
| 放出的东西成立 | 每个会离开原位的部件只含该物体、本体完整，颜色是颜料本色并与画中同类一致，在它经过的每种底上都成立；复制件看不出重复。 |

## 按问题找参考

| 遇到的问题 | 读或执行 | 注意 |
|---|---|---|
| 读图与关系描述库 | 规则 1–2；`references/reading.md` | 从画面证据建立关系，不由词库发明表情或事件 |
| 平台水印（小红书等） | `picture.py dewatermark --help`；`scripts/lp/watermark.py` | 标志和 ID 文字同在才自动处理；修复不是复原，按对比图核对 |
| Mac 上的识别起点（主体、脸与眼、肢体、焦点、文字） | `picture.py vision --help`；`references/macos-vision.md` | 每条都是起点：看 review.jpg，按原生尺寸核对后再粘进 plan 或 scene |
| iPhone 照片、HEIC、相机 RAW、广色域；没装 ffmpeg 出视频 | `references/macos-media.md` | prepare 统一转成 sRGB 工作图（RAW 在 Mac 上用系统解码器冲洗）；出视频时 ffmpeg 缺失或坏掉会自动改用 AVFoundation，超过 4096 像素的边改用 HEVC |
| 安装到 Claude Code 与 Codex、命令行、批量处理 | `bin/living-picture --help`、`doctor`；`references/invocation.md` | 一份正本，用符号链接安装；批量处理用 `workflows/living-picture-batch.js`；送图给 Codex 只在明确同意时 |
| 准备与选区 | `picture.py prepare/look --help`；`references/cutting.md` | 先核原画是否有可分边界，再选提取方法；坐标属于工作图 |
| 补底与重新显露 | `references/cutting.md`；`references/reveal-contract.md` | 选区不等于语义保证，补底不等于隐藏的真相；联合显露范围要验证 |
| scene 与动作字段 | `references/motion.md` | 一个时钟；参数示例不替代材料、连接与可见强度的判断 |
| 放飞散落元素（花瓣、雪、叶、鸟） | `references/motion.md` 第 7 节；`references/reading.md` | 取物体本体与颜料本色；复制受原件形状数限制；逐件验收 |
| 依赖、父子与深度 | `references/motion.md`；`runtime/src/03-layers.js` | 刚体可完整继承；变形附着是近似；复杂关节、闭环、反射耦合不自动支持 |
| 安静段、暂停与唤醒 | `references/runtime-rest-contract.md`；`runtime/src/04-mount.js` | 不猜字段；`seek(0)` 与原图回退分开 |
| 固定层与透明度 | `references/motion.md`；`scripts/lp/bundle.py` | 固定区只是不透明原图上的原像素硬蒙版；缩放边缘另验 |
| 观察与捕获 | `references/checking.md`；`node scripts/capture.mjs --help` | 主导动态全程、变化最大处、联合视角和实际尺寸；帧条不替代连续节奏 |
| 网页接入 | `references/embedding.md` | 原图回退、单实例、减少动态、正确销毁 |

交接时至少记下：原图身份、工作尺寸、画意和因果、关系、资产处理、动态与镜头参数、工具版本、验证范围和未决；如果触发规则第 8 条的真冲突，记下试过的替代、哪一项承载画意、公开让了什么。

## 文件

- `bin/living-picture`：统一的命令行入口（`new`、`doctor`、`paths`、`capture`、`codex`，以及 picture.py 的全部命令）；`scripts/install.sh` 用符号链接把这一份装给 Claude Code、Codex（`~/.agents/skills`）和命令行，默认只预演
- `scripts/picture.py`：`prepare`、`look`、`cut`、`bundle`、`diff`、`sheet`、`measure`、`palette`、`inspect`、`vision`、`dewatermark`（各有 `--help`）
- `scripts/capture.mjs`：从打包好的页面取帧和短视频（有 ffmpeg 用它，没有就用 AVFoundation）
- `scripts/lp/`：切层、补底、打包等工具代码；`tools.py` 是测量与检查；`watermark.py` 去平台水印；`apple.py` 调用 Mac 自带的 Vision
- `scripts/macos/`：按需编译的 Swift 小工具（Vision 识别、ImageIO 读图、AVFoundation 出视频），缓存在 `~/Library/Caches/living-picture`
- `scripts/codex_bridge.py`：可选的 Codex 独立复审与补底指引；不带 `--consent-upload` 什么都不发送
- `workflows/living-picture-batch.js`：多张图并行的工作流（制作、独立审查、修复）
- `runtime/src/`：播放器源码，`bundle` 时拼成 `living-picture.js`
- `references/`：读画、切层、运动词汇、检查、网页接入、补底和安静段两份合同，以及 Mac 识别（`macos-vision.md`）、Mac 读图与出视频（`macos-media.md`）、安装与调用（`invocation.md`）
- `examples/night-house/`：一张用脚本画的小图的完整示例（`make_picture.py` 画图，附 plan 和 scene），用来看文件格式怎样跑通。它是一张画的答案，不是模板。
