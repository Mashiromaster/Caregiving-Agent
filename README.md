# 守望者 Caregiving Agent

守望者是一个本地运行的看护 Agent 原型：以 **DeepSeek Harness（DSH）** 提供对话、工具调用和定时任务入口，以 **FallGuard** 负责摄像头监测、事件判定、留证、取消告警和外呼。你可以在对话中查询状态、开启监测或安排定时任务，也可以在同一界面的「守望者监控」面板查看实时画面与事件记录。

> 当前是单摄像头原型，尚未完成真实养老场景的误报率、漏报率和长期可靠性验证。请先用演练模式联调，不能将它作为唯一的紧急救助渠道。

## 界面与运行原理

```text
浏览器：DeepSeek Harness Web（127.0.0.1:3080）
├─ 官方对话界面、会话、模型工具调用、定时任务
└─ 守望者监控（侧栏入口 + /caregiving/ 面板）
   └─ caregiving-fallguard：DSH Host 插件
      ├─ 注册 fallguard_* 对话工具和系统提示
      ├─ 启动 / 停止本地 FallGuard Python 进程
      ├─ 代理监控面板并将新事件摘要回报给相关对话
      └─ FallGuard Runtime
         ├─ 摄像头 / 感知插件 → 跌倒与求助判定
         ├─ 分级、留证、取消窗口、事件归档
         └─ 确定性处置 → 外呼服务商
```

对话部分使用 DSH 自带的 Web 界面，所以外观和交互来自 Harness；它不是 DeepSeek 消费者网站的直接嵌入。「守望者监控」是本项目的页面，通过 DSH 的客户端扩展加入侧栏和主面板。这里的**插件**指一段按 DSH 扩展接口注册的程序：它给 Agent 增加可调用的 `fallguard_*` 工具、页面路由和工作进程，并不意味着把视频逐帧交给大模型判断。

实时监测和外呼由 FallGuard Runtime 执行；DSH 负责让用户通过对话操作和查询。监测中的告警处置采用确定性规则，模型不可用时仍能按已配置的处置链运行。新事件摘要会回报给**最近使用过 FallGuard 工具的对话**，不是向所有会话广播。

## 快速开始

需要 Node.js、npm、Python 3.13，以及 Python 的 `opencv-python` 和 `mediapipe`。在仓库根目录运行：

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install opencv-python mediapipe
cd eldercare_ai/dsh
npm ci
python run.py
```

浏览器打开 <http://127.0.0.1:3080>。在 DSH 设置中登录 DeepSeek 账号或配置 API Key，再选择包含定时任务工具的标准预设。`run.py` 使用仓库锁定的 `@deepseek-ai/dsh` 版本启动 Host，并加载守望者插件；保持这个进程运行，监测和定时任务才能执行。只想启动服务而不自动打开浏览器，可用 `python run.py --no-open`。

首次使用时，先点左侧「守望者监控」→「配置」，核对视频源、场景、联系人和外呼通道，再到「体检」检查环境。打开页面本身不会连接摄像头；点击「启动监测」或执行摄像头体检时才会连接。macOS 首次使用需要在「系统设置 → 隐私与安全性 → 摄像头」为启动服务的终端授权。

可以在对话中输入：

- “查看守望者监测状态和最近十条事件。”
- “启动跌倒监测。”
- “停止跌倒监测。”
- “每天早上 8 点启动监测，晚上 8 点停止，时区用 Asia/Shanghai。”

定时任务由**运行中的 DSH Host**触发。停止 `run.py` 后，Host 和 FallGuard 都停止；重启后，任务是否补发取决于 DSH 的任务规则。

## 外呼：先看清结果含义

默认配置为 `call.dry_run = true`，此时只演练外呼，不会实际拨号。配置项可在「守望者监控 → 配置」修改，也可参考 [`config.example.toml`](eldercare_ai/fall_guard/config.example.toml)。本地 `config.toml` 含联系人与凭据，已被 Git 忽略。

| 结果 | 含义 |
| --- | --- |
| `DRY_RUN` | 演练成功；没有向外呼服务商发起真实请求。 |
| `SUBMITTED` | 请求已提交给服务商；**不代表电话已响铃、接通或有人接听**。 |
| `CONFIG_MISSING` / 失败信息 | 通道、凭据或请求未通过检查；到「体检」和「事件记录」查看原因。 |

真实外呼需配置受支持的服务商、有效凭据和联系人，将演练模式关闭，并重新启动监测使配置生效。服务商的发送记录和接听状态需要在其平台核对。检测误报也可能触发真实外呼，因此请先在受控场景中验证阈值、取消窗口与联系人链。不要把页面上的 `CALLED` 当作已接通证明。

## 摄像头断开后如何恢复

macOS 的 iPhone 连续互通相机断开、重新连接后，系统可能重新分配摄像头编号。在「实时监控 → 视频源」尝试 `0` 或 `1`，然后重新点击「启动监测」；也可以填 `demo` 做无摄像头演练。先到「体检」查看实际可用视频源。若仍无法读帧，检查摄像头权限，以及视频会议软件或其他监控进程是否占用设备。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| [`eldercare_ai/dsh/run.py`](eldercare_ai/dsh/run.py) | 启动官方 DSH Web Host 并装载插件。 |
| [`eldercare_ai/dsh/fallguard-plugin.mjs`](eldercare_ai/dsh/fallguard-plugin.mjs) | 注册对话工具、监控页代理、工作进程和事件回报。 |
| [`eldercare_ai/dsh/lib/client.js`](eldercare_ai/dsh/lib/client.js) | 将「守望者监控」加入 DSH 侧栏与主面板。 |
| [`eldercare_ai/fall_guard/`](eldercare_ai/fall_guard/) | 摄像头感知、判定、告警、外呼及事件证据。 |

详细使用说明见 [DSH 入口文档](eldercare_ai/dsh/README.md) 和 [FallGuard 文档](eldercare_ai/fall_guard/README.md)。独立运行 FallGuard 可在激活虚拟环境后进入其目录，执行 `python main.py doctor` 或 `python main.py run --source demo`。

## 验证与边界

从仓库根目录运行软件自检：

```bash
cd eldercare_ai/fall_guard
../../.venv/bin/python tools/selftest.py
```

本机 Python 3.13 自检结果：**245 项通过，0 项失败**。自检覆盖代码路径，不等于真实照护场景的安全验证。已有少量公开跌倒片段测试，但缺少完整日常活动数据；实际摄像头使用中也出现过误报。当前系统主要依赖视觉，暗光、遮挡、被褥覆盖和多人同框都会影响判定；多人时仅跟踪面积最大的目标。可穿戴设备与个体健康基线有软件接口，尚无真实设备的长期验证。本地事件证据未加密，当前入口默认仅供本机使用，不应直接暴露到公网。

## 参考与许可

- [DeepSeek Harness 项目](https://github.com/deepseek-ai/deepseek-harness)、[架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、[插件开发](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/index.md)、[定时任务](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/schedule.md)
- 本项目采用 [MIT 许可证](LICENSE)。
