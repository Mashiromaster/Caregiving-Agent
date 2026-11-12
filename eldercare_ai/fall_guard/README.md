# 守望者 FallGuard · 摄像头跌倒预警系统

用普通电脑摄像头做跌倒检测，判定成立后由看护智能体自动拨打电话通知护理方与家属。
是配套智能看护方案中 L3 处置链路的可运行原型。

```
摄像头 → 姿态后端 → 判定状态机 → 留证 → 本地告警 → 倒计时取消窗口
                                                        ↓ 未取消
                              事件归档 ← 智能体处置（外呼/播报/派单）
                                            ↓ 失败
                                       确定性兜底强制外呼
```

## 快速开始

```bash
cd fall_guard
pip3 install opencv-python          # 必需
pip3 install mediapipe              # 推荐，骨骼点判定比兜底方案准得多

python3 main.py web                 # Web 控制台（推荐，配置/监控/复核都在里面）
python3 main.py doctor              # 环境体检
python3 main.py run --source demo   # 合成场景自检，无需摄像头
python3 main.py run                 # 摄像头实时监测
```

macOS 首次运行会弹窗请求摄像头授权；若没弹，去 系统设置 → 隐私与安全性 → 摄像头 勾选你的终端。

配置：用 `main.py web` 里的表单填，或复制 `config.example.toml` 为 `config.toml` 手改。不配也能跑，直接用示例配置。

## Web 控制台

```bash
python3 main.py web                 # 默认 http://127.0.0.1:8777，自动开浏览器
python3 main.py web --port 9000 --no-browser
```

五个页面：

| 页面 | 作用 |
| --- | --- |
| 总览 | 当前配置摘要 + 处置链路哪一环还缺东西 |
| 实时监控 | 带判定叠加层的画面、逐帧特征读数、运行日志，以及取消/标注/手动触发三个按钮 |
| 事件记录 | 事件列表、关键帧、现场短片、处置时间线、外呼结果、误报标注 |
| 配置 | 全部配置项的表单，带校验与外呼测试 |
| 体检 | `doctor` 的结构化版本，每项给出可操作的修复建议 |

网页上的「取消外呼 / 标注误报 / 手动触发」走的是与预览窗按键完全相同的代码路径（按键码注入 `_handle_key`），不是另一套实现。

两件要知道的事：

- **面板没有鉴权**。默认只监听 `127.0.0.1`。用 `--host 0.0.0.0` 让护士站远程访问，等于把监控画面和住户信息暴露给同网段任何人。
- **保存配置会丢注释**。TOML 的注释不是数据，`tomllib` 读进来就没了，面板是整体改写文件。保存前会自动留一份 `config.toml.bak`，示例注释在 `config.example.toml` 里。

配置页里的密钥字段建议填 `"env:ALIYUN_ACCESS_KEY_ID"` 这样的占位符——面板读写的是**未解析**的原始 TOML，占位符会原样保留，真实密钥不会被明文写进配置文件。

## 预览窗按键

| 键 | 作用 |
| --- | --- |
| `q` | 退出 |
| `c` | 倒计时内取消外呼（同时自动标注为误报） |
| `f` | 把最近一次事件标注为误报，并立即解除静默期 |
| `t` | 手动触发一次事件，用于演练外呼链路 |

无预览窗（`--headless`）时，倒计时内在终端按回车同样可取消。

## 判定逻辑

两条路径，对应技术文档里的 R-FALL-01/03：

| 路径 | 条件 | 等级 | 置信度 |
| --- | --- | --- | --- |
| `descent+lying` | 质心下降速率 ≥0.25/s **且** 随后保持躺倒 ≥2.5s 且体动低于阈值 | L3 | 0.72~0.97 |
| `lying_only` | 未捕捉到下降过程，但持续躺倒 ≥8s 且体动低 | L2 | 0.55 |

躺倒的判据优先用躯干与竖直方向夹角（≥60°），无骨骼点时退化为人体框宽高比（≥1.25）。

**怎么区分"主动躺下"和"跌倒"**：缓慢坐下的下降速率达不到阈值；主动躺卧后人会翻身调整，体动能量高于 `static_motion_max`。两者都只会走低置信路径或不触发。床铺等躺倒属正常的区域，用 `exclude_zones` 画框排除。

调阈值用 `tools/trace.py`，它逐帧打印所有判定特征：

```bash
python3 tools/trace.py --source demo
python3 tools/trace.py --source 你录的视频.mp4
```

## 姿态后端

| 后端 | 依赖 | 骨骼点 | 适用 |
| --- | --- | --- | --- |
| `mediapipe` | `pip3 install mediapipe` | 有 | **推荐**，单人场景，CPU 实时 |
| `yolo` | `pip3 install ultralytics`（含 torch，约 2GB） | 有 | 多人场景精度最好 |
| `motion` | 无 | 无 | 零依赖兜底，仅人体框 |

`backend = "auto"` 时按上表顺序挑第一个可用的。`motion` 后端依赖摄像头固定不动（它会冻结背景模型，否则长时间不动的躺倒者会被学进背景而消失）。

## 外呼配置

### 先选通道：有没有营业执照决定了路线

国内语音外呼受运营商实名制管控，**云厂商的语音通知一律要求企业实名认证 + 营业执照 + 话术报备**，个人账号开不了。这不是某一家的策略，阿里云、腾讯云、火山引擎都一样。

| 通道 | 个人能否开通 | 适用 |
| --- | --- | --- |
| `spug` | **可以**，微信扫码即用 | 个人、小团队、原型验证 |
| `aliyun` / `tencent` / `volc` | 需企业资质 | 机构正式部署 |

`spug` 走的是 [Spug 推送助手](https://push.spug.cc/)——这类聚合平台用自己的企业资质承载合规。代价是按次计费比直连云厂商贵、多一层服务商依赖、文案渲染在平台侧。对告警通知这个用途够用，但别拿它做营销外呼，会被封号。

### Spug（个人路线）

1. [push.spug.cc](https://push.spug.cc/) 微信扫码登录
2. 新建通知模板，通道选「电话」，内容写成 `#name#于#time#在#place#检测到跌倒，请立即前往`（变量名与 `param_keys` 对应）
3. 复制模板编码（调用 URL 里 `/send/` 后面那段）填进 `call.spug.template_id`

填了 `template_id` 就能用 `targets` 动态指定被叫号码，一个模板服务整条升级链。若只填 `user_id` 走 `/xsend` 简易接口，被叫人由 Spug 后台绑定决定，**此时联系人列表与升级链顺序都不生效**。

### 云厂商（企业路线）

签名全部用标准库手写，不引入云厂商 SDK。

```bash
export ALIYUN_ACCESS_KEY_ID=LTAI...        # 阿里云
export ALIYUN_ACCESS_KEY_SECRET=...
export VOLC_ACCESS_KEY=AKLT...             # 火山引擎
export VOLC_SECRET_KEY=...
```

三家的模板都要在各自控制台申请并等审核通过（1-3 个工作日），**变量格式各不相同**：

| 通道 | 模板变量格式 | 关键配置 | 变量传递方式 |
| --- | --- | --- | --- |
| `aliyun` | `${name}` | `tts_code` | `TtsParam` JSON |
| `tencent` | 模板内置顺序 | `template_id` + `voice_sdk_app_id` | `TemplateParamSet` **按顺序** |
| `volc` | `#name#` | `tts_resource_key` + `number_pool_no` | `PhoneParam` JSON |

```
阿里云：${name}于${time}在${place}检测到跌倒，请立即前往
火山：  #name#于#time#在#place#检测到跌倒，请立即前往
```

照搬另一家的模板会播成空值。火山的变量名还不能含中文、区分大小写。火山另有一处与别家不同：外呼幂等由 `SingleOpenId` 保证，系统用「事件号 + 号码后四位」作为该 ID，所以同一起事件对同一个人不会因重试而打两通电话。

### 通用

**`call.dry_run = true` 是默认值**，此时只打印请求参数不真实拨号。先这样把链路跑通，参数确认无误后再改 `false`。

```bash
python3 main.py call-test            # 演练，看参数
python3 main.py call-test --real     # 真实拨号，会产生费用
```

控制台的「配置 → 外呼链路测试」是同一个东西，还会把完整请求参数展开给你看。

注意**演练一定返回成功**：`dry_run` 分支在校验凭据之前就返回了，所以演练通过不代表密钥和模板没问题。要确认凭据，看 `doctor` 或体检页的「通道凭据」一项。

系统能提供的槽位只有 `name / time / place / event` 四个（见 `runtime._slots()`），模板变量超出这个集合会播成空值。腾讯云的 `template_param_keys` 是**按顺序**填充 `TemplateParamSet` 的，顺序错了播报内容会串位。

播报内容强制包含**位置、姓名、事件、时间**四要素——夜间被叫醒的护工要能在 5 秒内听懂发生了什么。

## 智能体

`agent.enabled = true` 且配了 `api_key` 时，由 LLM 通过 function-calling 编排处置；否则走确定性规则 planner。两者调用同一套工具。

```bash
export DEEPSEEK_API_KEY=sk-xxx
python3 main.py run
```

默认 DeepSeek，改 `agent.base_url` 可指向任意 OpenAI 兼容端点（vLLM、Ollama、本地推理服务）。

工具集：`get_contacts` / `get_evidence` / `speak` / `notify_desktop` / `place_call` / `finish`。智能体只能通过这些工具影响世界，拿不到网络、文件系统和 shell。

**三条护栏写在代码里而不是提示词里**，所以模型超时、胡说、欠费、API 挂掉都不会导致"没人去救"：

1. `deadline_seconds` 到点立即中止循环
2. 循环结束后若一通电话都没打出去，兜底逻辑按升级链强制外呼
3. 工具层面拦截：未知联系人、重复呼叫、超出外呼上限一律拒绝

这条路径有测试覆盖，也实测过：用无效 api_key 运行时 LLM 返回 401，兜底立刻接管完成了外呼。

## 事件记录与误报标注

```bash
python3 main.py events                      # 列表
python3 main.py events --detail EV2026...   # 完整 JSON
python3 main.py label EV2026... --false --reason "护工搬动"
python3 main.py label EV2026... --true
```

`data/events.jsonl` 只追加不修改，标注写入独立的 `data/labels.jsonl`，读取时合并——原始记录始终可审计。

每起事件在 `data/evidence/<事件号>/` 下留存：

```
keyframe_t-3.0s.jpg    跌倒前 3 秒（应为站立，用于复核下降过程）
keyframe_t-2.0s.jpg
keyframe_t-1.0s.jpg
keyframe_t+0.0s.jpg    判定瞬间
keyframe_t+2.0s.jpg
keyframe_t+5.0s.jpg
clip.mp4               前 5 秒 + 后 10 秒
event.json             完整事件记录（含每次工具调用的输入输出与耗时）
```

## 常用命令

```bash
python3 main.py web                     # Web 控制台
python3 main.py web --host 0.0.0.0      # 允许远程访问（无鉴权，谨慎）
python3 main.py run --no-call           # 强制演练，不真实拨号
python3 main.py run --no-agent          # 关掉 LLM，只用确定性处置
python3 main.py run --quiet             # 静音，不播报不响铃
python3 main.py run --headless          # 不开预览窗（服务器部署）
python3 main.py run --backend motion    # 强制指定后端
python3 main.py run --cancel-seconds 30 # 调试时把取消窗口放长
python3 main.py run --max-seconds 300   # 跑 5 分钟自动退出

python3 tools/selftest.py               # 自检（55 项，不需摄像头与网络）
python3 tools/trace.py --source demo    # 逐帧打印判定特征
```

## 项目结构

```
fallguard/
  config.py        配置加载，env: 占位符解析
  capture.py       视频源（摄像头/文件/合成）+ JPEG 滚动缓冲
  pose/            姿态后端：mediapipe / yolo / motion
  detector.py      判定状态机
  render.py        预览窗渲染
  events.py        事件日志与误报标注
  alerts/
    local.py       语音播报、告警音、倒计时取消窗口
    evidence.py    关键帧与短视频留证
    call.py        阿里云 / 腾讯云外呼（手写签名）
  agent/
    tools.py       工具集与 schema
    harness.py     function-calling 循环 + 规则 planner + 兜底
  web/
    server.py      HTTP 服务与 API 路由（标准库 http.server）
    schema.py      配置表单描述、类型强制、校验
    tomlwrite.py   TOML 写入器（stdlib 的 tomllib 只能读）
    runner.py      后台运行器：MJPEG 推流 + 网页按键注入
    doctor.py      结构化体检
    static/        前端单页，无构建步骤
  pipeline.py      主流水线
```

## 已知边界

这是单摄像头原型，与配套技术文档中描述的正式方案的差距在于：

- **只有视觉单源**，没有毫米波雷达做交叉验证。完全黑暗、被褥覆盖、家具遮挡场景下会漏报，卫生间也无法部署——这正是正式方案用雷达的原因。
- **无个体基线建模**，阈值全院统一而非按人设定。技术文档里把个体基线列为误报率从 1.2 降到 0.27 次/床·月的首要贡献项，这里没有。
- **误报率未经真实场景验证**。合成场景与自检只能证明逻辑正确，不能替代真实机构的长期运行数据。
- **无多人跟踪**，画面内多人时只取面积最大的目标。
- 本地留证无加密，不满足等保要求；正式部署需要按技术文档第 9 章补齐。

换句话说：这套原型能证明"检测到跌倒 → 自动把电话打出去"这条链路成立，但距离"误报率低到护工不会关掉它"还有距离，而后者才是产品能否落地的真正门槛。
