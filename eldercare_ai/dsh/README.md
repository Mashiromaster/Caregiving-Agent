# 守望者 Agent：DeepSeek Harness 入口

## 架构

```text
浏览器（DeepSeek Harness Web，127.0.0.1:3080）
  ├─ 对话、模型工具循环、会话、定时任务
  └─ 「守望者监控」侧栏 → /caregiving/ 同源面板
       └─ caregiving-fallguard Host 插件
            ├─ 管理 FallGuard 工作进程、工具调用、事件回报
            └─ Python Runtime：感知插件 → 判定/分级 → 留证 → 取消窗口
                 └─ 确定性处置 → 外呼网关 → 事件归档
```

Harness 是应用 Host，插件负责启动、停止和回收 FallGuard 工作进程，并把监控页挂到同源路径 `/caregiving/`。浏览器只访问 Harness 入口；监控页需要 Harness 浏览器登录态，工作进程使用随机本机端口和临时凭据。摄像头判定、取消窗口与告警外呼不依赖模型是否在线。事件归档后，插件会把摘要送到最近操作监测的 DSH 对话中。`call.dry_run = true` 时只演练。

## 启动

需要 Node.js、npm、Python 3.13，以及 FallGuard 的 `opencv-python`。在本目录运行：

```bash
npm ci
python3.13 run.py
```

打开 `http://127.0.0.1:3080`，在 Harness 设置中登录 DeepSeek 账号或配置 API Key。左侧的「守望者监控」打开实时画面、住户配置与事件证据。建议使用标准预设，它包含内置的 `schedule_*` 工具。在对话中可以说：

- “查看跌倒监测状态和最近十条事件”
- “启动跌倒监测”
- “停止跌倒监测”
- “每天早上 8 点启动跌倒监测，晚上 8 点停止，使用 Asia/Shanghai 时区”

Harness 的定时任务由运行中的 Host 触发。关掉 `run.py` 后监测服务和 Harness 一起停止，定时任务会保留，重新启动 Host 后按 Harness 的补发规则处理。

首次使用前，在 `http://127.0.0.1:3080/caregiving/` 配置场景、联系人和外呼通道。确认外呼配置后再关闭演练模式。Harness 模型工具仅获得状态与事件摘要，不读取密钥或住户原始视频。

打开页面不会启用摄像头；点击「启动监测」或在「体检」中检查视频源时才会连接。macOS 的摄像头授权和打开操作在 Python 主线程执行。iPhone 连续互通相机断开或重连后，系统可能重新分配摄像头编号；若当前编号无法读帧，可在「实时监控 → 视频源」临时尝试 `0`、`1`，或用 `demo` 做无摄像头演练。启动失败后无需重启 Harness，可更换视频源并再次启动。

## 边界

- 独立运行 FallGuard 时，原有的 `fallguard/agent/harness.py` 仍可用于事件处置。由 DSH 启动时只使用 DSH 的模型循环；实时告警按确定性规则处置，以保证模型故障时仍能外呼。
- 跌倒判定与拨号是异步现实动作。`fallguard_start` 只表示监测线程启动，不能表示已经检测到跌倒或电话接通。外呼结果请查询事件记录。
- DSH 入口默认仅监听本机。面板代理使用 Harness 的 Host、Origin 和登录校验。FallGuard 工作进程额外验证临时凭据；不要把这套原型直接部署为公网服务。

官方参考：[Harness 架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、[插件教程](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/index.md)、[定时任务](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/schedule.md)。
