# RemoveBlack — 桌面图片去底工具

把"24bit 黑底图"或"任意纯色背景图"一键转换为"32bit 带透明通道 PNG"，**视觉零损失**。

灵感来源：[抠出黑底图片的彩色部分并使其视觉正确 — VikingWei](https://zhuanlan.zhihu.com/p/1967271925005919701)，
即 AE 中 **UnMult** 插件的算法：

```
A   = max(R, G, B)
RGB = RGB / A
```

合成回黑底时 `RGB' × A == RGB`，与原图视觉完全一致。

---

## 功能特性

- 拖拽图片 / 文件夹到窗口即可处理
- 支持 **Ctrl+V 粘贴图片**（截图、剪贴板图片）
- **六种算法**可切换：
  - **UnMult（黑底）**（推荐，对应原文方案）
  - **UnMult（吸管背景色）**：用吸管吸取绿幕/蓝幕等任意纯色背景
  - **背景色键控（保色）**：按与背景色的距离直接映射 alpha，保留原 RGB，适合对色彩准确度要求高的场景
  - 阈值法（适合干净纯黑底）
  - 颜色键（保留半透明边缘 / 发光特效）
  - HSV 去色背景
- 棋盘格透明背景预览，参数滑块实时调整
- 保护画笔 / 魔棒 / 吸管等编辑工具
- 批量处理整个文件夹，带进度条 / 可取消
- 把图片或文件夹直接拖到 `.exe` 图标上也能用

---

## 开发环境运行

```bash
python -m pip install -r requirements.txt

# GUI
python -m src.app

# CLI
python -m src.cli path\to\img.png
python -m src.cli path\to\folder --algo unmult
python -m src.cli imgs --algo chroma --lower 8 --upper 64 --recursive --out out_dir
python -m src.cli img.png --algo unmult --strength 1.5 --body-density 0.6 --alpha-floor 20
python -m src.cli img.png --algo unmult_color --bg-color 0,255,0 --strength 1.2 --body-density 0.3
python -m src.cli img.png --algo color_key --bg-color 0,255,0 --lower 8 --upper 64
python -m src.cli img.png --algo hsv --hue 120 --hue-tolerance 20
```

## 跑测试

```bash
python tests\test_algorithms.py
# 或
python -m pytest tests/
```

## 一键打包

```bash
build.bat
```

输出：

```
dist\RemoveBlack.exe         GUI 单文件，免安装
dist\RemoveBlack-vX.X.X.exe  带版本号副本
```

---

## 算法说明

| 算法 | 公式 | 适用场景 |
|---|---|---|
| **UnMult（黑底）** | `A = max(R,G,B)`；`RGB /= A` | 黑底特效 / 火焰 / 烟雾 / 粒子 / 技能图 |
| **UnMult（吸管背景色）** | `A = max\|C - B\| / 255`；`C_fg = B + (C - B) / A` | 绿幕 / 蓝幕 / 任意纯色背景 |
| **背景色键控（保色）** | `A = (max\|C - B\| - lower) / (upper - lower)`；保留原 RGB | 绿幕 / 蓝幕 / 需要保留原色的场景 |
| Threshold | 亮度 ≤ 阈值 → 透明 | 干净纯黑底 |
| Chroma key | `lower~upper` 之间线性渐变 alpha | 需要保留边缘半透明 |
| HSV key | 按目标色相范围去除背景 | 纯色背景（绿幕 / 蓝幕等）|

> ⚠️ UnMult 会改变原始 RGB 通道（除以了 alpha），如果该图后续要被当 mask 用，请改用 Threshold。

---

## 项目结构

```
RemoveBlack/
├── src/
│   ├── core/
│   │   ├── algorithms.py     # 核心算法（UnMult / UnMult Color / 背景色键控 / 阈值 / 颜色键 / HSV）
│   │   └── processor.py      # 单图 / 批量调度
│   ├── gui/
│   │   ├── widgets.py        # 棋盘背景 + 拖拽区 + 保护蒙版绘制 + 吸管
│   │   └── main_window.py    # 主窗口
│   ├── cli.py                # CLI 入口
│   └── app.py                # GUI 入口
├── tests/test_algorithms.py
├── .github/workflows/tests.yml  # CI
├── requirements.txt
├── build.bat                 # PyInstaller 打包脚本
└── README.md
```
