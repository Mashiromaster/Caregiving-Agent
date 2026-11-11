# -*- coding: utf-8 -*-
"""配置表单的字段描述、类型强制与校验。

一份声明同时服务三件事：前端表单渲染、提交值类型强制、保存前校验。
分成 errors / warnings 两级——errors 阻止保存（写进去系统跑不起来），
warnings 允许保存但提示（比如真实拨号模式下 TTS 模板还是占位值）。

读写用的是**未解析**的原始 TOML：config.py 加载时会把 "env:XXX" 替换成
环境变量值，面板若拿解析后的数据回写，密钥就会被明文落盘。所以这里
始终直接 tomllib 读文件、保留 env: 占位符原样写回。
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

from . import tomlwrite

FILE_HEADER = """守望者 FallGuard 配置 · 由 Web 控制台生成
形如 "env:XXX" 的值会在运行时从同名环境变量读取，密钥不要直接写在这里。
注意：本文件由面板整体改写，手写的注释不会保留（示例注释见 config.example.toml）。
"""

# 外呼模板可用的变量，由 pipeline._slots() 决定
SLOT_KEYS = ("name", "time", "place", "event")

WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass
class Field:
    key: str
    label: str
    type: str                      # text | int | float | bool | choice | lines | floats
    help: str = ""
    choices: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    unit: str = ""
    placeholder: str = ""
    wide: bool = False


@dataclass
class Section:
    name: str
    title: str
    desc: str = ""
    fields: list[Field] = dc_field(default_factory=list)
    kind: str = "table"            # table | rows（表数组，如 [[call.contacts]]）
    path: str = ""                 # kind=rows 时的点号路径


SECTIONS: list[Section] = [
    Section("scene", "场景", "外呼播报四要素里的「姓名 + 位置」取自这里，建议姓名脱敏。", [
        Field("resident_name", "住户姓名", "text", "语音播报用，建议写成「张**」", wide=True),
        Field("place", "位置", "text", "护工听到后要能直接找到人，写到房间号", wide=True),
        Field("institution", "机构名称", "text", "仅用于记录", wide=True),
    ]),
    Section("camera", "视频源", "", [
        Field("source", "视频源", "text",
              "摄像头序号(0/1) | demo 合成自检 | screen[:N] 屏幕捕获 | 视频文件路径",
              placeholder="0", wide=True),
        Field("width", "宽", "int", min=160, max=4096, unit="px"),
        Field("height", "高", "int", min=120, max=2160, unit="px"),
        Field("fps", "帧率", "int", min=1, max=60, unit="fps"),
        Field("flip", "左右镜像", "bool", "画面镜像翻转"),
        Field("screen_crop", "屏幕裁剪", "floats",
              "仅 source=screen 时生效，归一化 x,y,w,h 四个值；留空为整屏",
              placeholder="0.0, 0.0, 0.5, 1.0", wide=True),
    ]),
    Section("detector", "判定阈值",
            "调阈值前先用「实时监控」页看特征读数，或跑 tools/trace.py 逐帧打印。", [
        Field("backend", "姿态后端", "choice",
              "auto 按 mediapipe→yolo→motion 挑第一个可用的",
              choices=("auto", "mediapipe", "yolo", "motion")),
        Field("min_person_score", "人体置信度下限", "float", min=0.0, max=1.0),
        Field("lying_angle_deg", "躺倒角度阈值", "float",
              "躯干与竖直方向夹角 ≥ 此值视为躺倒（0=站立，90=水平）",
              min=0.0, max=90.0, unit="°"),
        Field("lying_aspect", "躺倒宽高比", "float",
              "无骨骼点时的兜底判据：人体框宽高比 ≥ 此值视为躺倒", min=0.1, max=10.0),
        Field("floor_y", "地面高度线", "float",
              "质心归一化纵坐标 ≥ 此值才算在地面高度；设 0 关闭此条件",
              min=0.0, max=1.0),
        Field("descent_rate_min", "下降速率阈值", "float",
              "缓慢坐下达不到此速率，这是区分「主动躺下」的主要判据",
              min=0.0, max=5.0, unit="/s"),
        Field("descent_window_s", "下降统计窗口", "float", min=0.2, max=10.0, unit="s"),
        Field("lying_seconds", "双条件保持时长", "float",
              "捕捉到下降过程后，再保持躺倒多久确认（高置信路径）",
              min=0.1, max=120.0, unit="s"),
        Field("lying_only_seconds", "单条件保持时长", "float",
              "未捕捉到下降过程时，单纯躺倒多久确认（低置信兜底路径）",
              min=0.5, max=600.0, unit="s"),
        Field("lying_grace_s", "躺倒计时宽限", "float",
              "姿态估计在地面姿态上会抖动甚至丢帧，躺倒计时允许中断这么久而不清零",
              min=0.0, max=30.0, unit="s"),
        Field("lying_min_density", "躺倒帧占比下限", "float",
              "宽限期内躺倒帧占比低于此值，视为姿态不稳而非持续躺倒",
              min=0.0, max=1.0),
        Field("static_motion_max", "静止体动上限", "float",
              "躺倒期间平均体动能量超过此值视为主动躺卧；主动躺卧后人会翻身调整",
              min=0.0, max=1.0),
        Field("cooldown_seconds", "静默期", "float",
              "一次事件后多久内不再触发，避免同一起跌倒重复外呼",
              min=0.0, max=3600.0, unit="s"),
        Field("exclude_zones", "排除区域", "zones",
              "床铺等躺倒属正常的区域，在这些框内不触发", wide=True),
    ]),
    Section("evidence", "留证", "", [
        Field("dir", "存放目录", "text", "相对路径基于 fall_guard/ 目录", wide=True),
        Field("pre_seconds", "回溯时长", "int", "保存跌倒前多少秒", min=0, max=60, unit="s"),
        Field("post_seconds", "后置时长", "int", "继续保存跌倒后多少秒", min=0, max=120,
              unit="s"),
        Field("keyframe_offsets", "关键帧时间点", "floats",
              "相对判定时刻的秒数，逗号分隔", placeholder="0.0, 2.0, 5.0", wide=True),
    ]),
    Section("alert", "本地告警与取消窗口",
            "取消窗口是压制误报扰人的关键：窗口内可人工撤销，走完才真正外呼。", [
        Field("cancel_seconds", "取消窗口", "float",
              "判定成立后留多少秒给现场撤销；设 0 则立即外呼", min=0.0, max=300.0,
              unit="s"),
        Field("speak", "本机语音播报", "bool"),
        Field("alarm_sound", "本机告警音", "bool"),
        Field("voice", "语音音色", "text", "macOS say 的音色名，中文可填 Tingting；留空用系统默认"),
        Field("suppress_call_windows", "抑制时段", "lines",
              "这些时段只记录不外呼（如查房时间），每行一条 HH:MM-HH:MM，支持跨午夜",
              placeholder="06:30-07:30", wide=True),
    ]),
    Section("agent", "智能体",
            "不配 api_key 会降级为确定性规则处置；无论哪条路径，兜底逻辑都保证至少一通电话打出去。", [
        Field("enabled", "启用 LLM 编排", "bool"),
        Field("base_url", "API 端点", "text",
              "OpenAI 兼容端点，可指向 vLLM / Ollama / 本地推理服务", wide=True),
        Field("api_key", "API Key", "text",
              '建议填 "env:DEEPSEEK_API_KEY"，实际值放环境变量', wide=True),
        Field("model", "模型", "text", wide=True),
        Field("temperature", "temperature", "float", min=0.0, max=2.0),
        Field("max_steps", "最大步数", "int", min=1, max=50),
        Field("deadline_seconds", "处置时限", "float",
              "智能体未在此时限内完成，确定性兜底立即接管", min=1.0, max=300.0, unit="s"),
    ]),
    Section("call", "外呼", "", [
        Field("provider", "外呼通道", "choice", choices=("none", "aliyun", "tencent")),
        Field("dry_run", "演练模式", "bool",
              "开启时只打印请求参数不真实拨号。联调通过后再关闭"),
        Field("play_times", "播报次数", "int", "同一通电话里重复播报几遍", min=1, max=5),
        Field("max_calls_per_event", "单次事件外呼上限", "int", min=1, max=10, unit="通"),
        Field("call_all_contacts", "呼叫全部联系人", "bool",
              "关闭=呼通一个即停；开启=按顺序全部呼叫"),
    ]),
    Section("call.contacts", "联系人", "按顺序构成升级链；caregiver 能最快到现场，排在 family 前面。",
            [
                Field("id", "标识", "text", "智能体按此 id 指定呼叫对象，须唯一",
                      placeholder="nurse_station"),
                Field("name", "名称", "text", placeholder="值班护士站"),
                Field("phone", "号码", "text", placeholder="13800000001"),
                Field("role", "角色", "choice", choices=("caregiver", "family", "doctor",
                                                         "manager")),
            ], kind="rows", path="call.contacts"),
    Section("call.aliyun", "阿里云语音通知",
            "dyvmsapi SingleCallByTts。需在控制台申请 TTS 模板并等审核通过（1-3 个工作日）。", [
        Field("access_key_id", "AccessKeyId", "text",
              '建议填 "env:ALIYUN_ACCESS_KEY_ID"', wide=True),
        Field("access_key_secret", "AccessKeySecret", "text",
              '建议填 "env:ALIYUN_ACCESS_KEY_SECRET"', wide=True),
        Field("called_show_number", "主叫号码", "text", "已认证的外显号码，留空用系统默认",
              wide=True),
        Field("tts_code", "TTS 模板 Code", "text", "控制台审核通过的模板 Code",
              placeholder="TTS_1234567", wide=True),
        Field("tts_param_keys", "模板变量名", "lines",
              "与模板里的 ${变量} 逐一对应，每行一个。可用：" + " / ".join(SLOT_KEYS),
              wide=True),
    ]),
    Section("call.tencent", "腾讯云语音消息", "vms SendTtsVoice。", [
        Field("secret_id", "SecretId", "text", '建议填 "env:TENCENT_SECRET_ID"', wide=True),
        Field("secret_key", "SecretKey", "text", '建议填 "env:TENCENT_SECRET_KEY"',
              wide=True),
        Field("region", "地域", "text", placeholder="ap-guangzhou", wide=True),
        Field("voice_sdk_app_id", "VoiceSdkAppid", "text", wide=True),
        Field("template_id", "模板 ID", "text", wide=True),
        Field("template_param_keys", "模板变量名", "lines",
              "腾讯云按**顺序**填充 TemplateParamSet，顺序错了播报内容会串位。可用："
              + " / ".join(SLOT_KEYS), wide=True),
    ]),
    Section("log", "日志", "", [
        Field("dir", "事件日志目录", "text", "events.jsonl / labels.jsonl 的存放位置",
              wide=True),
    ]),
]


def schema_json() -> list[dict[str, Any]]:
    """给前端渲染表单用的 schema。"""
    out = []
    for sec in SECTIONS:
        out.append({
            "name": sec.name,
            "title": sec.title,
            "desc": sec.desc,
            "kind": sec.kind,
            "path": sec.path or sec.name,
            "fields": [{
                "key": f.key, "label": f.label, "type": f.type, "help": f.help,
                "choices": list(f.choices), "min": f.min, "max": f.max,
                "unit": f.unit, "placeholder": f.placeholder, "wide": f.wide,
            } for f in sec.fields],
        })
    return out


# --------------------------------------------------------------- 读写
def config_path(explicit: str | None = None) -> Path:
    pkg_root = Path(__file__).resolve().parent.parent.parent
    if explicit:
        return Path(explicit).expanduser().resolve()
    return pkg_root / "config.toml"


def read_raw(explicit: str | None = None) -> dict[str, Any]:
    """读未解析的配置；config.toml 不存在时回落到 config.example.toml。"""
    path = config_path(explicit)
    if not path.exists():
        example = path.parent / "config.example.toml"
        path = example if example.exists() else path
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def write_raw(data: dict[str, Any], explicit: str | None = None) -> Path:
    """整体改写配置文件，写前留一份 .bak。"""
    path = config_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_bytes(path.read_bytes())
    text = tomlwrite.dumps(data, header=FILE_HEADER)
    # 写临时文件再替换：保存过程中进程被杀不会留下半截配置
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


# --------------------------------------------------------------- 类型强制
class CoerceError(ValueError):
    pass


def _num(value: Any, kind: str) -> Any:
    if isinstance(value, bool):
        raise CoerceError("需要数字")
    if isinstance(value, (int, float)):
        return int(value) if kind == "int" else float(value)
    text = str(value).strip()
    if not text:
        raise CoerceError("不能为空")
    try:
        return int(text, 10) if kind == "int" else float(text)
    except ValueError:
        # "15.0" 填进整数字段是常见情形，按四舍五入收下
        if kind == "int":
            try:
                return int(round(float(text)))
            except ValueError:
                pass
        raise CoerceError(f"不是合法的{'整数' if kind == 'int' else '数字'}：{text!r}")


def _split_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[\n,，;；]", str(value))
    return [s.strip() for s in items if s.strip()]


def coerce(f: Field, value: Any) -> Any:
    if f.type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}
    if f.type in {"int", "float"}:
        out = _num(value, f.type)
        if f.min is not None and out < f.min:
            raise CoerceError(f"不得小于 {f.min:g}")
        if f.max is not None and out > f.max:
            raise CoerceError(f"不得大于 {f.max:g}")
        return out
    if f.type == "choice":
        text = str(value).strip()
        if text not in f.choices:
            raise CoerceError(f"只能是 {', '.join(f.choices)} 之一")
        return text
    if f.type == "lines":
        return _split_list(value)
    if f.type == "floats":
        out = []
        for item in _split_list(value):
            try:
                out.append(float(item))
            except ValueError:
                raise CoerceError(f"不是合法的数字：{item!r}")
        return out
    if f.type == "zones":
        if not isinstance(value, (list, tuple)):
            raise CoerceError("需要区域数组")
        zones = []
        for i, zone in enumerate(value, 1):
            nums = zone if isinstance(zone, (list, tuple)) else _split_list(zone)
            if len(nums) != 4:
                raise CoerceError(f"第 {i} 个区域需要 4 个数值（x1,y1,x2,y2）")
            try:
                vals = [float(v) for v in nums]
            except (TypeError, ValueError):
                raise CoerceError(f"第 {i} 个区域含非数字")
            if any(v < 0.0 or v > 1.0 for v in vals):
                raise CoerceError(f"第 {i} 个区域的坐标须为 0~1 的归一化值")
            if abs(vals[2] - vals[0]) < 1e-6 or abs(vals[3] - vals[1]) < 1e-6:
                raise CoerceError(f"第 {i} 个区域面积为 0")
            zones.append(vals)
        return zones
    # text
    return "" if value is None else str(value)


def _camera_source(value: Any) -> Any:
    """摄像头序号存成整数，其余按字符串。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 10) if text.isdigit() else text


def _dig(data: dict[str, Any], path: str) -> dict[str, Any]:
    node = data
    for part in path.split("."):
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    return node


def _peek(data: dict[str, Any], path: str) -> Any:
    """按点号路径取值，不存在返回 None（不像 _dig 那样建空表）。"""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# --------------------------------------------------------------- 校验
def validate(payload: dict[str, Any], *, base: dict[str, Any] | None = None
             ) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """强制类型并校验。

    返回 (合并后的完整配置, errors, warnings)。errors 非空时不应保存。
    base 为文件里已有的内容——面板不认识的键原样保留，不被抹掉。
    """
    data: dict[str, Any] = _deep_copy(base or {})
    errors: list[dict[str, str]] = []

    for sec in SECTIONS:
        # 节名是点号路径（call.aliyun），而表单提交的是嵌套结构，不能用平键取
        incoming = _peek(payload, sec.name)
        if incoming is None:
            continue

        if sec.kind == "rows":
            rows_in = incoming if isinstance(incoming, list) else []
            rows_out: list[dict[str, Any]] = []
            for idx, row in enumerate(rows_in):
                if not isinstance(row, dict):
                    continue
                clean: dict[str, Any] = {}
                for f in sec.fields:
                    if f.key not in row:
                        continue
                    try:
                        clean[f.key] = coerce(f, row[f.key])
                    except CoerceError as exc:
                        errors.append({"field": f"{sec.name}[{idx}].{f.key}",
                                       "label": f"{sec.title} 第 {idx + 1} 行 · {f.label}",
                                       "message": str(exc)})
                if any(str(v).strip() for v in clean.values()):
                    rows_out.append(clean)
            parent_path, _, leaf = sec.path.rpartition(".")
            parent = _dig(data, parent_path) if parent_path else data
            parent[leaf] = rows_out
            continue

        target = _dig(data, sec.name)
        for f in sec.fields:
            if f.key not in incoming:
                continue
            raw = incoming[f.key]
            # 可选字段留空时删掉键，让 DEFAULTS 生效（screen_crop 必须如此，
            # 否则空数组会被 open_source 当成「配了但不合法」）
            if f.key in {"screen_crop"} and not _split_list(raw):
                target.pop(f.key, None)
                continue
            if sec.name == "camera" and f.key == "source":
                target[f.key] = _camera_source(raw)
                continue
            try:
                target[f.key] = coerce(f, raw)
            except CoerceError as exc:
                errors.append({"field": f"{sec.name}.{f.key}", "label": f.label,
                               "message": str(exc)})

    errors.extend(_cross_checks(data))
    return data, errors, _warnings(data)


def _deep_copy(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _deep_copy(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_deep_copy(v) for v in node]
    return node


def _cross_checks(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def err(field: str, label: str, message: str) -> None:
        out.append({"field": field, "label": label, "message": message})

    contacts = (data.get("call") or {}).get("contacts") or []
    seen: set[str] = set()
    for idx, c in enumerate(contacts):
        label = f"联系人第 {idx + 1} 行"
        cid = str(c.get("id") or "").strip()
        phone = str(c.get("phone") or "").strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        if not cid:
            err(f"call.contacts[{idx}].id", label, "标识不能为空，智能体靠它指定呼叫对象")
        elif cid in seen:
            err(f"call.contacts[{idx}].id", label, f"标识 {cid!r} 重复")
        else:
            seen.add(cid)
        if not phone:
            err(f"call.contacts[{idx}].phone", label, "号码不能为空")
        elif len(digits) < 7:
            err(f"call.contacts[{idx}].phone", label, f"号码位数不对：{phone!r}")

    for idx, win in enumerate((data.get("alert") or {}).get("suppress_call_windows") or []):
        m = WINDOW_RE.match(str(win))
        ok = False
        if m:
            sh, sm, eh, em = (int(v) for v in m.groups())
            ok = sh <= 23 and eh <= 23 and sm <= 59 and em <= 59
        if not ok:
            err(f"alert.suppress_call_windows[{idx}]", "抑制时段",
                f"格式应为 HH:MM-HH:MM：{win!r}")

    det = data.get("detector") or {}
    try:
        if float(det.get("lying_only_seconds", 8)) <= float(det.get("lying_seconds", 2.5)):
            err("detector.lying_only_seconds", "单条件保持时长",
                "应大于双条件保持时长，否则低置信路径会先于高置信路径触发")
    except (TypeError, ValueError):
        pass

    ev = data.get("evidence") or {}
    for key, label in (("keyframe_offsets", "关键帧时间点"),):
        for off in ev.get(key) or []:
            if float(off) < 0:
                err(f"evidence.{key}", label, "时间点不能为负（回溯帧是自动保存的）")
                break
    return out


def _warnings(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    call = data.get("call") or {}
    provider = str(call.get("provider") or "none").lower()
    dry = bool(call.get("dry_run", True))
    contacts = call.get("contacts") or []

    if not contacts:
        out.append("没有配置任何联系人，判定成立后无人可呼——系统会走完整链路但不会打电话。")
    elif not any(str(c.get("role")) == "caregiver" for c in contacts):
        out.append("联系人里没有 caregiver 角色。升级链会退化为按配置顺序呼叫，"
                   "最先到现场的人不一定排在前面。")

    if provider == "none":
        out.append("外呼通道为 none，只会记录事件不会拨号。")
    if provider == "aliyun":
        ali = call.get("aliyun") or {}
        code = str(ali.get("tts_code") or "")
        if not code or code.startswith("TTS_0000"):
            out.append("阿里云 TTS 模板 Code 还是占位值，真实拨号会被拒绝。"
                       "需在控制台申请语音通知模板并等审核通过。")
        extra = [k for k in (ali.get("tts_param_keys") or []) if k not in SLOT_KEYS]
        if extra:
            out.append(f"模板变量 {', '.join(extra)} 不在系统可提供的槽位里"
                       f"（{'/'.join(SLOT_KEYS)}），播报时会是空值。")
    if provider == "tencent":
        ten = call.get("tencent") or {}
        if not str(ten.get("template_id") or "") or not str(ten.get("voice_sdk_app_id") or ""):
            out.append("腾讯云模板 ID 或 VoiceSdkAppid 未填，真实拨号会被拒绝。")
        extra = [k for k in (ten.get("template_param_keys") or []) if k not in SLOT_KEYS]
        if extra:
            out.append(f"模板变量 {', '.join(extra)} 不在系统可提供的槽位里"
                       f"（{'/'.join(SLOT_KEYS)}），播报时会是空值。")
    if not dry:
        out.append("演练模式已关闭：下一次判定成立会真实拨号并产生费用。")

    alert = data.get("alert") or {}
    try:
        if float(alert.get("cancel_seconds", 10)) <= 0:
            out.append("取消窗口为 0，误报将没有人工撤销的机会，直接外呼。")
    except (TypeError, ValueError):
        pass

    agent = data.get("agent") or {}
    if bool(agent.get("enabled")) and not str(agent.get("api_key") or ""):
        out.append("智能体已启用但未填 api_key，会降级为确定性规则处置（兜底外呼仍然生效）。")

    det = data.get("detector") or {}
    if str(det.get("backend")) == "motion":
        out.append("motion 后端没有骨骼点，判定退化为人体框宽高比，且要求摄像头固定不动。")
    return out
