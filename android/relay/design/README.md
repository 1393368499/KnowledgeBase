# 智库随行 App 图标

白色翻开的书页与三段语音波形合成一个标记；蓝色沿用现有 App，浅青色强调中心语音入口。不使用小字，在桌面小尺寸保持可辨识。

- `launcher-icon.png`：1024px 蓝底图标设计源图。
- `launcher-emblem-master.png`：内置 image_gen 生成的原始透明标记，保留原文件。
- `../res/`：安卓自适应图标、单色主题层、圆形及五种密度的回退图标。
- 适配层 432px = 108dp @ xxxhdpi；可见视口为中间72dp，标记保留在中心安全区。
- 通过官方 aapt2 编译、链接，Manifest 引用启动图标和 roundIcon；沿用原 APK 签名。

## 生成方式与最终提示词

使用内置 image_gen（透明背景），未使用 API/CLI。生成后只作安卓资源所需的等比缩放、透明留白和密度导出。

Use case: logo-brand. Create ONE polished Android launcher foreground icon for 智库随行, a Chinese personal knowledge-base and voice-task companion app. Asset: a single standalone emblem on a genuinely transparent background, square 1024 by 1024 composition. Brand palette is the existing app's royal blue #2856E8 with white; foreground emblem predominantly crisp solid white, with one restrained pale-cyan accent. Design one unified, memorable geometric symbol that merges an open book with a three-bar voice waveform in the central spine, suggesting knowledge that listens and accompanies the user. The book's two gently outward-swept pages feel like a compact speech bubble; the waveform is simple and bold. Precise optical balance, broad clean shapes, generous clear negative space, premium flat graphic, extremely readable at 48 pixels. Symbol centered, occupying about 56 percent of canvas width, within Android adaptive-icon central safe zone. No text, no letters, no wordmark, no tile/background plate, no border, no mockup, no phone, no glow, no drop shadow, no gradients or 3D, no tiny ornamental details. Output only the single emblem with transparent alpha.
