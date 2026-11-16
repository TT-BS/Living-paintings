# 嵌入与生命周期

`bundle`产生自包含`out/index.html`和需HTTP服务的`out/site/`。工作原图为无损`picture.png`，`scene.original`引用它；图层编码是否无损在assetContract中记录。原图回退与图层合成各自验证。

## 手动挂载

```html
<div id="art" style="position:relative">
  <img src="art/picture.png" alt="画面说明" style="display:block;width:100%;height:auto">
</div>
<script src="art/living-picture.js"></script>
<script>
fetch('art/scene.json').then(r => {
  if (!r.ok) throw new Error('Scene unavailable');
  return r.json();
}).then(scene => {
  const picture = LivingPicture.mount('#art', scene, {base:'art/'});
  picture.ready.catch(error => console.warn(error));
});
</script>
```

若scene引用hooksId，先加载bundle生成的hooks.js，再挂载。自包含页已经处理hooks与assets，不要重复执行第二次挂载。组件离开时调用`LivingPicture.unmount(element)`；它也取消尚未完成的autoMount请求。手动fetch的取消由组件负责，已卸载组件不能再mount。

## 状态约定

- pause/reset恢复工作原图并复位；play从明确的初始状态继续。seek/advance/snapshot是确定性验证接口，不代表用户暂停。
- 初始减少动态不加载动画层、不建立GL；原图正常解码。运行中偏好切换取消等待并释放图形资源。媒体恢复、context恢复和迟到请求都受实例生命周期约束。
- 隐藏或离屏停止实时绘制，保留当前状态；这不同于用户暂停回原图，也不表示初始离屏挂载零加载。
- 安静段的停算与唤醒遵守[runtime-rest-contract.md](runtime-rest-contract.md)；只有工具实际支持的场景声明可用，不能把任意低速运动判作静止。
- 透明动画显示canvas时隐藏底下原图，避免重复alpha覆盖；回退时恢复原图。第三方页面不要另加第二份动态叠层。

## 在实际页面验收

核容器比例、布局变动、目标显示尺寸、DPR、按钮可达性、无指针时的主导动态、减少动态、暂停重播、离屏返回和快速组件切换。确认同一画面只有一个存活实例。GL或资产失败时原图仍应可读；失败不自动算动画交付完成。以画意选择的小幅或分布变化仍须在实际容器尺寸可察觉，不因桌面原生图可见就宣称手机通过。

初始原图和动画资产的网络成本分开记录。内联文件已经携带素材，不能因少调用loadImage而声称没有传输。保留实际可用的无损回退，不以压缩后的预览图替代它。
