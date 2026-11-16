# 显式安静段：原图休息与停算合同

播放器允许场景声明短暂的安静段。这里写接口、边界和用法；它不会自动发现安静，也不证明画面或节奏好。

## 已实现什么

允许场景作者明确标出短安静段，并提供检查函数。进入已声明且入口检查通过的段后，播放器显示原有无损原图 img，隐藏 canvas、停止 RAF，也不继续推进模拟；只保留一个段末定时器。到期后，使用原来的 1/60 秒步长补算未推进的状态，不绘制被跳过的画面，再继续同一个 `world.time`。RNG、通道、事件及角色状态走现有 step，未建立另一套动画时钟。

**这支持显式安静段，不自动发现安静。** 不根据 blink、wander 或对称动作“看起来会回零”推断整张画静止。没有 `scene.rest` 的场景照常运行，不强制隐藏已有空间和连续微动。

```js
scene.rest = { windows: [[2, 5], [8, 10]] };
scene.restCheck = function (world, LP) {
  // 必须检查本场景实际使用的姿态、相机、光影等中性条件。
  // 此处是作者的确定性验证逻辑，不是库自动生成的通用静止判定。
  return world.poseIsOriginal === true;
};
```

| 接口 | 精确含义 |
| --- | --- |
| `rest.windows` | 单位为场景秒；`[start,end)`；按时间排序、互不重叠；边界须是 1/60 秒整数倍。每段最多 10 秒；不支持自动循环或无限段。 |
| `restCheck(world, LP)` | 必需的纯、确定性函数，只能返回 `true` 或 `false`。不得读墙钟、DOM，不得消耗 RNG 或改变状态。 |
| `instance.resting` | 当前显示经过安静段检查的原图；可包括 scripted 静态预览及离屏暂停，此字段不等于一定有活动 timer。 |
| `instance.restUntil` | 当前安静段末的场景时间；不在安静状态时为 `null`。 |
| `restChecks / restDeclines` | 检查次数及入口拒绝次数，供诊断，不是画面质量分数。 |
| `instance.wake()` | 先将睡眠中的逻辑状态补算到当前已过去的时间，取消 timer，禁止本段再次入睡。不会覆盖用户 pause、reduced 或 scripted 状态。返回是否可接受后续操作。 |

缺 checker、畸形/重叠/超过 10 秒/非步长对齐的窗口，在挂载产生资源前明确拒绝。checker 返回非布尔值视作错误。初始化后的错误通过 `instance.error` 和 `onError`/警告报告；调用方不能只看 `ready` 已完成。

## 声明与证据的边界

1. 作者的窗口声明意味着：**这整个区间在没有打断时允许呈现原画，下一次可见事件不早于段末。** 库不能从一帧推出这个未来承诺。
2. 真实播放在入口核验。返回 `false` 时保留正常动态，记录 `restDeclines`，不盖掉尚未结束的姿态或空间交互。
3. 已入睡期间不运行 checker，也不推进状态。故不能说是“实时持续证明”。唤醒补算时逐步复核段内状态；发现中途声明失实，会明确报错、停 RAF/timer、回原图。已经隐藏过的错误区间无法事后撤销。
4. `seek/advance` 对经过的未被打断的声明区间逐步严格检查，可用于上线前预检。发现不成立就抛错，不把它算作合格休息。它只证明已运行的种子、脚本与时间范围。
5. 验证应依据驱动画面的实际语义状态，包括生效姿态、相机、光照、sprite/flow/ripple 以及当前交互。未被画面使用的非零通道不必归零；反之“通道为零”也不能单独证明所有运动中性。`pos/fullXf/carry` 等上一次 render 的缓存不能充当未绘制步骤的姿态证据。
6. 本合同不生成动作的收束，也不把弹簧渐近零强行解释成精确零。若作者无法写出可信检查，保持连续播放，不声明该段。

原图来自页面上已有的回退 img；构建器输出的无损 PNG 与原像素保真由切层工具负责（见 reveal-contract.md）。runtime 在休息时切回这个真实来源，没有用图层合成“近似原图”。这不证明任意调用方提供的 img 本身就正确。未声明窗口中的 `seek(0)` 仍是图层合成；不能把它与原图休息混称。

## 唤醒、取消和资源上限

- 到段末：一个 timer 唤醒，补算该段剩余状态，显示下一事件并恢复 RAF；相邻短段可以再次入睡。
- 指针进入画面、`tap/gust/puff/ripple/setPointer`、有效滚动变化以及显式 `wake()`：先补算到当前逻辑时刻再施加事件，并禁止当前窗口再次入睡，避免刚响应又被盖回原图。
- 自定义外部事件应先调用 `wake()` 再更改状态；直接在睡眠期间改 `world` 或 `world.ch.fire()` 不会自动通知调度器。
- 暂停、reset、destroy：立即取消 timer，不为已放弃的睡眠补算。销毁后的迟到回调通过票据和 disposed 状态拒绝。
- reduced 和 context loss：取消 timer、回原图；恢复后按现有重建语义从零初始化。隐藏页签/离屏则先补算已过去的那部分、取消 timer；剩余场景时间随不可见状态冻结，恢复可见时再安排剩余段。
- 单次睡眠最多 10 秒，即最多 600 个补算 step。迟到很久的 timer 也只推进到声明段末，不追赶整段墙钟。更长安静可由作者拆段，但每段末有一次补算，**不能称整个长区间零 CPU**。
- 补算仍是同步执行的有限工作，不是解析跳跃或后台线程；复杂自定义 tick/checker 可能耗时。600 步上限不保证任意场景的毫秒级性能。主要节省的是休息期间 RAF、渲染、几何上传与持续轮询；不能承诺所有模拟 CPU 都被省掉。

`world.time` 在睡眠期间是最后已求值的逻辑时间，直到唤醒才推进；不要把它当持续更新的墙钟显示。现有连续场景的普通 RAF 时钟行为未改变。`advance()` 是明确的确定性手动求值入口，现在会停活动 timer/RAF 并进入 scripted 模式；`play()` 才恢复真实播放。

## 可集成的小例子

以下仅展示接口，不是新的审美方案。假定只有 `part` 这一个刚体动作，其他图层静止，无 flow/sprite/ripple/额外光效；画面原件已通过原有构建提供。2–5 秒明确为原姿态，5 秒后下一次动作开始。验证实际驱动字段，不靠未使用的 blink/wander 是否为零。

```js
scene.wind = { breeze: 0, gust: false, pointer: 0 };
scene.camera = { travel: 0.025, drift: 0, scroll: 0 };
scene.light = {};
scene.rest = { windows: [[2, 5]] };
scene.setup = function (w) { w.appliedDx = 0; };
scene.tick = function (w, t) {
  const frame = Math.round(t * 60);
  const quiet = frame >= 120 && frame < 300;
  // 作者的真实动作计划必须确实在这段回原姿态；不能由 restCheck 假造。
  const phase = frame < 120 ? frame / 120 : (frame - 300) / 120;
  w.appliedDx = quiet ? 0 : 2 * Math.sin(Math.PI * phase);
  w.byId.part.xf = [1, 0, 0, 0, 1, 0, w.appliedDx, 0, 1];
};
scene.restCheck = function (w) {
  return w.appliedDx === 0 &&
    w.camera.x === 0 && w.camera.y === 0 &&
    w.pointer.want === 0 && w.pointer.active === 0 &&
    !w.light.shade && w.light.glows.length === 0 &&
    w.light.gain.every(v => v === 1) &&
    w.light.lift.every(v => v === 0);
};
```

已有 hooks 文件可将 `restCheck` 注册到 `LivingPictureHooks[scene.hooksId]`，与已有 setup/tick 同一注册路径；无需单独播放器。严格相机/指针条件不成立时拒绝休息并继续动态，示例没有掩盖弹簧尚未回原位的问题。

```js
const picture = LivingPicture.mount(host, scene, options);
await picture.ready;
if (picture.error) throw picture.error;

// 发布前对这段配置、种子进行确定性逐步预检。
picture.seek(5);
picture.seek(3);             // 原图可见，resting=true；没有 timer/RAF。
picture.snapshot();          // 导出原图，不重新绘制图层。
picture.play();              // 按当前逻辑时刻安排剩余窗口。

// 自定义外部事件：先与同一时钟同步，再操作通道。
if (picture.wake()) picture.world.ch.fire('attention');
```

若动态计划本来是连续麦浪、云或呼吸，不应为了套入此合同凭空插入空白段。应保留连续行为，或者另行获得作者认可的真实收束与安静计划。
