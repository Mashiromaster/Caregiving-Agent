# Caregiving Agent · 守望者 Guardian AI

面向养老机构的智能看护原型：DeepSeek Harness 提供对话与定时任务入口，
FallGuard 插件负责监测、事件处置与外呼。

```
DeepSeek Harness Host（对话 / 定时任务 / 守望者监控页）
   └─ caregiving-fallguard 插件（工具 / 工作进程 / 事件回报）
      └─ FallGuard Runtime：感知插件（跌倒 / 求助）→ 分级 → 留证 → 取消窗口
         └─ 确定性处置 → 外呼网关 → 事件归档
```

## 仓库内容

| 目录 | 说明 |
| --- | --- |
| [`eldercare_ai/dsh/`](eldercare_ai/dsh/) | 官方 DeepSeek Harness 的对话入口与 FallGuard 插件。见[使用说明](eldercare_ai/dsh/README.md) |
| [`eldercare_ai/fall_guard/`](eldercare_ai/fall_guard/) | 跌倒监测、求助手势、健康信号、事件处置和外呼。见[子项目 README](eldercare_ai/fall_guard/README.md) |

配套的商业计划报告书、技术文档与融资演示文稿（含估值、财务预测与团队信息）
未纳入本公开仓库。

## 快速开始

```bash
cd eldercare_ai/dsh
npm ci
python3.13 run.py                 # 对话界面 http://127.0.0.1:3080
```

在 Harness 设置里登录 DeepSeek 账号或配置 API Key，然后通过对话启动监测或创建定时任务。「守望者监控」位于同一个界面的侧栏，面板路径为 `/caregiving/`。

单独运行 FallGuard：

```bash
cd eldercare_ai/fall_guard
pip3 install opencv-python mediapipe

python3 main.py doctor               # 环境体检
python3 main.py run --source demo    # 合成场景自检，无需摄像头
python3 main.py run                  # 摄像头实时监测
python3 tools/selftest.py            # 自检 245 项
```

macOS 首次运行需在 系统设置 → 隐私与安全性 → 摄像头 授权终端。

## 验证状态

在 [UR Fall Detection Dataset](http://fenix.ur.edu.pl/~mkepski/ds/uf.html)
（Rzeszów 大学公开研究数据集）的真人跌倒序列上实测：

| 项目 | 结果 |
| --- | --- |
| 真人跌倒序列检出 | 3/3，每段各触发一次 |
| 误报（20s 静止站立 + 跌倒前片段） | 0 |
| 自检套件 | 245 项全通过（2026-11-12，本机 Python 3.13） |
| LLM 不可用时的兜底外呼 | 实测 401 后兜底接管成功 |

**验证缺口（重要）**：数据集服务器限流，未能取得日常活动（ADL）序列，
因此**坐下、弯腰、躺床这些最易误报的动作尚未覆盖**。现有「0 误报」
结论的样本范围有限，不能代表真实机构场景的误报率。

## 已知边界

这是单摄像头视觉原型，与文档中描述的正式方案差距在于：

- **跌倒检测主要依赖视觉**，无毫米波雷达交叉验证。黑暗、被褥覆盖、遮挡场景会漏报，卫生间无法部署。可穿戴数据接入和个体健康基线已实现软件接口，尚无真实设备长期验证。
- **无多人跟踪**，画面内多人时只取面积最大的目标。
- 本地留证未加密，不满足等保要求。

## 文档数据口径

本系统的配套商业与技术文档未包含在本仓库中。文档中的公司名、团队背景与
联系方式为示例占位，市场数据来自国家统计局与民政部公开统计公报，
试点数据与财务预测为示例性测算。

## 许可

[MIT](LICENSE)
