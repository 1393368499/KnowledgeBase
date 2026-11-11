---
name: 智库
description: 用深青索引架、浅青工作底和白色阅读面组织资料查证。
colors:
  rail: "#133b39"
  rail-hover: "#22534f"
  rail-text: "#f1faf6"
  rail-muted: "#bdd3cb"
  accent: "#166557"
  accent-hover: "#0e4e44"
  accent-soft: "#e0f2e9"
  focus: "#146e60"
  background: "#eff4f2"
  surface: "#ffffff"
  subtle: "#f5f8f6"
  text: "#172c29"
  text-secondary: "#4b625d"
  border: "#cedbd5"
  border-strong: "#7d968d"
  divider: "#e0e8e4"
  success: "#246342"
  success-soft: "#edf7ef"
  warning: "#785414"
  warning-soft: "#fff5df"
  error: "#a03636"
  error-soft: "#fbeeee"
  scrim: "rgb(14 36 33 / .48)"
typography:
  display:
    fontFamily: 'system-ui, "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif'
    fontSize: "2rem"
    fontWeight: 660
    lineHeight: 1.35
    letterSpacing: "-.025em"
  headline:
    fontSize: "2rem"
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: "-.025em"
  title:
    fontSize: "1.125rem"
    lineHeight: 1.35
  body:
    fontFamily: 'system-ui, "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif'
    fontSize: "14px"
    lineHeight: 1.65
  reading:
    fontSize: ".9375rem"
    lineHeight: 1.85
  label:
    fontSize: ".75rem"
    lineHeight: 1.65
  mono:
    fontFamily: "ui-monospace, Consolas, monospace"
  metric:
    fontSize: "1.375rem"
    fontWeight: 680
    lineHeight: 1.3
rounded:
  compact: "2px"
  tag: "4px"
  control: "6px"
  brand: "8px"
  surface: "12px"
spacing:
  compact: "6px"
  row: "8px"
  grid: "12px"
  section: "16px"
  inset: "18px"
  wide: "24px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "5px 12px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "5px 12px"
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "5px 12px"
  button-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.error}"
    rounded: "{rounded.control}"
    padding: "5px 12px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "6px 10px"
  navigation-item:
    backgroundColor: "{colors.rail}"
    textColor: "{colors.rail-text}"
    rounded: "{rounded.control}"
    padding: "5px 10px"
  navigation-item-selected:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.rail}"
  status-tag:
    backgroundColor: "{colors.subtle}"
    textColor: "{colors.text-secondary}"
    typography: "{typography.label}"
    rounded: "{rounded.tag}"
    padding: "2px 7px"
  status-tag-success:
    backgroundColor: "{colors.success-soft}"
    textColor: "{colors.success}"
  status-tag-warning:
    backgroundColor: "{colors.warning-soft}"
    textColor: "{colors.warning}"
  status-tag-error:
    backgroundColor: "{colors.error-soft}"
    textColor: "{colors.error}"
  card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.surface}"
    padding: "18px"
  composer:
    backgroundColor: "{colors.subtle}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
  index:
    backgroundColor: "{colors.subtle}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
    padding: "14px 16px"
---

# Design System: 智库

## Overview

**Creative North Star: "档案索引架"**

把工作区做成一间用于查证的档案室。深青导航标示当前位置，浅青工作底承接管理操作，白色阅读面让来源、会话与文档各自清楚可辨。

用分组、细分隔线和稳定的排版支持连续工作。读者可以快速扫过资料、筛选和操作，也能在同一套界面里从容阅读长文。

**Key Characteristics:**

- 深青定位，浅青组织，白面阅读。
- 方正分区、单线图标、对齐的等宽数字。
- 文字说明状态，颜色帮助识别。

## Colors

### Primary

- **档案深青**（`rail`）用于常驻导航、登录介绍与助手头像。栏内正文用浅白青（`rail-text`），组名和元数据用灰青（`rail-muted`）。
- **行动青**（`accent`）用于主按钮、链接和进行中进度条。悬停加深（`accent-hover`），浅青选中底（`accent-soft`）同时用于当前导航、选项和命中词。键盘焦点使用专用青色（`focus`）。

### Neutral

- **浅青工作底**（`background`）承接管理页。**阅读白**（`surface`）用于正文、表单和数据容器。**薄青衬底**（`subtle`）组织索引、表头和辅助区域。
- **墨青正文**（`text`）与**灰青次文**（`text-secondary`）区分内容和元数据。**边框**（`border`，对白 1.43:1）只用于分隔性线条；**交互控件边框**（`border-strong`，对白 ≥3:1）用于按钮、输入框、下拉与其他可操作容器——控件边界必须能被低视力用户看见（WCAG 2.2 SC 1.4.11）。**分隔线**（`divider`）划分连续内容。
- 完成、警告和失败各有深色文字与浅色底的语义色对。抽屉和模态层使用统一的深青遮罩（`scrim`）。

**The 状态带文字 Rule.** 用状态文字与语义色同时标示结果，保留失败原因和可见的恢复入口。

## Typography

界面和通用文档使用系统无衬线与中文字体回退。计数用等宽数字，端点、标识符与原件纯文本使用等宽字族。公文预览按模板使用方正小标宋、黑体、楷体与仿宋的本地回退。

- **入口与页面标题**（`2rem`、字重 `660` / `650`、行高 `1.35`）用紧凑字距。手机工作区标题收至 `1.55rem`。
- **卡片标题**（`1.125rem`）负责分组。**界面正文**（`14px` / `1.65`）与**阅读正文**（`.9375rem` / `1.85`）分别服务操作和长读。
- **辅助标签**（`.75rem`）承载分组、会话元数据和状态。**统计值**（`1.375rem`、字重 `680`）保持等宽数字对齐。

## Layout

- 桌面导航宽 `230px`，顶部栏高 `58px`，主区独立滚动。管理页内容宽至 `1536px`，页面边距以 `34px` 起步。表格与表单的筛选、说明和动作位于数据之前。
- 助手起始页将提问区与资料索引并列，内容最大宽度 `1170px`。会话阅读列最大宽度 `900px`，输入区回到会话底部。文档栏默认宽 `700px`，并排时为会话保留 `620px`。低于 `1200px` 时文档打开为全宽工作区。
- `960px` 及以下，侧栏切为带遮罩的抽屉，管理页横向边距收至 `18px`。`720px` 及以下，资料索引移至提问区下方。`600px` 及以下，管理页横向边距收至 `12px`，表格在容器内横向滚动，系统配置的操作列固定在末侧。登录页由双栏变为上下两段。
- `360px` 及以下，顶栏改为 `78px` 高的两行网格，环境状态独占第二行。常用控件之间使用 `6–12px` 的间隔，区块之间使用 `16–24px` 的留白。

## Elevation & Depth

界面以平面色块、`1px` 边框和分隔线建立层级。抽屉和模态窗使用遮罩，菜单靠边框与层叠位置浮在对应动作附近。提示条以短暂淡入和位移出现（`.16s ease`），处理中使用小型转环（`.7s linear`）。减少动态效果的系统偏好会关闭两种动画。

## Shapes

区域保持方正，圆角随容器尺寸增加。紧凑文档控件用 `2px`，标签用 `4px`，常用控件用 `6px`，品牌标记用 `8px`，卡片和提问框用 `12px`。圆形留给状态点、转环等小型指示。通用图标为 `18px` 单线 SVG，使用圆端和圆连接（描边 `1.65`）。

## Components

- **按钮**使用 `6px` 圆角与细边框。主操作为行动青底白字，次操作白底带边框，弱操作透明底，危险操作用红色文字。普通按钮至少高 `36px`，手机至少 `38px`，登录按钮高至少 `44px`。常规键盘焦点为 `2px` 描边，外偏移 `2px`，深青导航使用浅色焦点。
- **输入与范围**使用白底、细边框与可见标签。提问框以薄青衬底包住输入、资料范围和发送，聚焦时强调边框。范围回执留在同一组件内，随操作区换行。
- **导航与索引**按工作目的分组。当前入口用浅青底和深青字，悬停用深青栏的提亮层。索引用分隔行承载名称、计数和简短说明，移动端导航仍能访问最近会话与行内操作。
- **卡片与状态**使用白面、细边框和 `18px` 内距，手机收至 `14px`。表格按行分隔，状态标签保持小尺寸并写明结果。受控操作卡在等待确认与失败时同时改变边框和状态文案。
- **原件预览**把翻页与缩放控件留在画布上方。浅青画布允许双向滚动，白色原页保留实际缩放尺寸，文本预览保留原换行。

## Do's and Don'ts

### Do

- **Do** 用细分隔线组织资料、统计和操作，按标题、正文、元数据的层级排版。
- **Do** 保持来源、范围、状态和关键动作可见，并给键盘焦点留出空间。
- **Do** 把密集表格的横向滚动限制在表格容器内，并显示滚动提示。

### Don't

- **Don't** 用大面积强调色、渐变或厚重阴影为常规界面分区。
- **Don't** 只用颜色、悬停或图标传递状态和操作原因。
- **Don't** 在窄屏强行并排文档与会话，或把放大的原件压回画布宽度。
