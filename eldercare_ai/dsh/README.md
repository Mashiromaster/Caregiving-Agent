# 守望者 Agent：DeepSeek Harness 入口

## 架构

```text
浏览器（DeepSeek Harness Web，127.0.0.1:3080）
  └─ DeepSeek Harness：会话、模型工具循环、定时任务、对话记录
       └─ caregiving-fallguard 插件：status / start / stop / events
            └─ 本机 FallGuard API（127.0.0.1:8777）
                 └─ Python Runtime：感知插件 → 判定/分级 → 留证 → 取消窗口
                      └─ 智能体处置或确定性兜底 → 外呼网关
```

Harness 是对话与定时任务的主入口；FallGuard 是独立的长期监测服务。摄像头判定和取消窗口不依赖模型是否在线。事件成立后的外呼按 FallGuard 配置执行，`call.dry_run = true` 时只演练。前端实时画面、住户配置与事件证据仍在 `127.0.0.1:8777` 的专用面板。

## 启动

需要 Node.js、npm、Python 3.13，以及 FallGuard 的 `opencv-python`。在本目录运行：

```bash
npm ci
python3.13 run.py
```

打开 `http://127.0.0.1:3080`，在 Harness 设置中选择 DeepSeek 模型并配置 API Key。建议使用标准预设，它包含内置的 `schedule_*` 工具。在对话中可以说：

- “查看跌倒监测状态和最近十条事件”
- “启动跌倒监测”
- “停止跌倒监测”
- “每天早上 8 点启动跌倒监测，晚上 8 点停止，使用 Asia/Shanghai 时区”

Harness 的定时任务由运行中的 Host 触发。关掉 `run.py` 后监测服务和 Harness 一起停止，定时任务会保留，重新启动 Host 后按 Harness 的补发规则处理。

首次使用前，在 `http://127.0.0.1:8777` 配置场景、联系人和外呼通道。确认外呼配置后再关闭演练模式。Harness 插件仅访问本机 API，不直接读取密钥或住户原始视频。

## 边界

- 当前项目原有的 `fallguard/agent/harness.py` 是事件处置用的 DeepSeek API 工具循环；它不等于官方 DeepSeek Harness。Web 对话使用官方 `@deepseek-ai/dsh`，版本由 `package-lock.json` 固定。
- 跌倒判定与拨号是异步现实动作。`fallguard_start` 只表示监测线程启动，不能表示已经检测到跌倒或电话接通。外呼结果请查询事件记录。
- 两个服务都只监听本机。当前 FallGuard 原型面板没有账户鉴权，不应直接开放到公网。

官方参考：[Harness 架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、[插件教程](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/index.md)、[定时任务](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/schedule.md)。
