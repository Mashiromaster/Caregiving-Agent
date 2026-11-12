# -*- coding: utf-8 -*-
"""语音外呼：阿里云语音通知 / 腾讯云语音消息。

两家的签名算法都用标准库手写（阿里 RPC HMAC-SHA1、腾讯 TC3-HMAC-SHA256），
不引入云厂商 SDK——SDK 体积大、版本耦合强，而这里只需要一个接口。

dry_run=True 时打印完整请求参数但不发网络请求，便于在没有认证账号的阶段
把整条链路（判定 → 取消窗口 → 智能体 → 外呼 → 留痕）先跑通。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

TIMEOUT = 12


@dataclass
class Contact:
    id: str
    name: str
    phone: str
    role: str = "caregiver"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Contact":
        return cls(
            id=str(raw.get("id") or raw.get("phone", "")),
            name=str(raw.get("name") or "联系人"),
            phone=str(raw.get("phone") or "").strip(),
            role=str(raw.get("role") or "caregiver"),
        )


@dataclass
class CallResult:
    contact_id: str
    name: str
    phone: str
    role: str
    ok: bool
    provider: str
    dry_run: bool
    request_id: str = ""
    call_id: str = ""
    code: str = ""
    message: str = ""
    latency_ms: int = 0
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "name": self.name,
            "phone": _mask(self.phone),
            "role": self.role,
            "ok": self.ok,
            "provider": self.provider,
            "dry_run": self.dry_run,
            "request_id": self.request_id,
            "call_id": self.call_id,
            "code": self.code,
            "message": self.message,
            "latency_ms": self.latency_ms,
        }


def _mask(phone: str) -> str:
    """日志中不留完整号码。"""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) >= 7:
        return f"{digits[:3]}****{digits[-4:]}"
    return "***"


def _post_form(url: str, params: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]
               ) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ------------------------------------------------------------------ 阿里云
def _aliyun_encode(value: str) -> str:
    out = urllib.parse.quote(str(value), safe="")
    return out.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


class AliyunVoice:
    """dyvmsapi SingleCallByTts（语音通知·文本转语音）。"""

    name = "aliyun"
    endpoint = "https://dyvmsapi.aliyuncs.com/"
    version = "2017-05-25"

    def __init__(self, conf, play_times: int):
        self.key_id = str(conf.get("access_key_id") or "")
        self.key_secret = str(conf.get("access_key_secret") or "")
        self.show_number = str(conf.get("called_show_number") or "")
        self.tts_code = str(conf.get("tts_code") or "")
        self.param_keys = list(conf.get("tts_param_keys") or [])
        self.play_times = play_times

    def missing(self) -> list[str]:
        lack = []
        if not self.key_id:
            lack.append("ALIYUN_ACCESS_KEY_ID")
        if not self.key_secret:
            lack.append("ALIYUN_ACCESS_KEY_SECRET")
        if not self.tts_code or self.tts_code.startswith("TTS_0000"):
            lack.append("call.aliyun.tts_code")
        return lack

    def build_params(self, contact: Contact, slots: dict[str, str],
                     out_id: str) -> dict[str, str]:
        tts_param = {k: slots.get(k, "") for k in self.param_keys} if self.param_keys \
            else dict(slots)
        params = {
            "Action": "SingleCallByTts",
            "Version": self.version,
            "Format": "JSON",
            "RegionId": "cn-hangzhou",
            "AccessKeyId": self.key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "CalledNumber": contact.phone,
            "TtsCode": self.tts_code,
            "TtsParam": json.dumps(tts_param, ensure_ascii=False),
            "PlayTimes": str(self.play_times),
            "Volume": "100",
            "Speed": "-10",          # 语速放慢，夜间接听清晰度优先
            "OutId": out_id,
        }
        if self.show_number:
            params["CalledShowNumber"] = self.show_number
        return params

    def _sign(self, params: dict[str, str]) -> str:
        canonical = "&".join(
            f"{_aliyun_encode(k)}={_aliyun_encode(v)}"
            for k, v in sorted(params.items()))
        to_sign = f"POST&{_aliyun_encode('/')}&{_aliyun_encode(canonical)}"
        digest = hmac.new((self.key_secret + "&").encode(),
                          to_sign.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    def call(self, contact: Contact, slots: dict[str, str], out_id: str,
             dry_run: bool) -> CallResult:
        params = self.build_params(contact, slots, out_id)
        base = CallResult(contact_id=contact.id, name=contact.name, phone=contact.phone,
                          role=contact.role, ok=False, provider=self.name,
                          dry_run=dry_run, params={
                              k: v for k, v in params.items()
                              if k in {"CalledNumber", "TtsCode", "TtsParam",
                                       "PlayTimes", "CalledShowNumber"}})
        if dry_run:
            base.ok = True
            base.code = "DRY_RUN"
            base.message = "未实际拨号（call.dry_run=true）"
            return base
        lack = self.missing()
        if lack:
            base.code = "CONFIG_MISSING"
            base.message = "缺少配置：" + ", ".join(lack)
            return base

        t0 = time.time()
        try:
            signed = dict(params)
            signed["Signature"] = self._sign(params)
            resp = _post_form(self.endpoint, signed)
            base.latency_ms = int((time.time() - t0) * 1000)
            base.request_id = str(resp.get("RequestId", ""))
            base.call_id = str(resp.get("CallId", ""))
            base.code = str(resp.get("Code", ""))
            base.message = str(resp.get("Message", ""))
            base.ok = base.code == "OK"
        except Exception as exc:
            base.latency_ms = int((time.time() - t0) * 1000)
            base.code = "REQUEST_FAILED"
            base.message = f"{type(exc).__name__}: {exc}"
        return base


# ------------------------------------------------------------------ 腾讯云
class TencentVoice:
    """vms SendTtsVoice（语音消息·模板语音）。"""

    name = "tencent"
    host = "vms.tencentcloudapi.com"
    service = "vms"
    version = "2020-09-02"
    action = "SendTtsVoice"

    def __init__(self, conf, play_times: int):
        self.secret_id = str(conf.get("secret_id") or "")
        self.secret_key = str(conf.get("secret_key") or "")
        self.region = str(conf.get("region") or "ap-guangzhou")
        self.app_id = str(conf.get("voice_sdk_app_id") or "")
        self.template_id = str(conf.get("template_id") or "")
        self.param_keys = list(conf.get("template_param_keys") or [])
        self.play_times = play_times

    def missing(self) -> list[str]:
        lack = []
        if not self.secret_id:
            lack.append("TENCENT_SECRET_ID")
        if not self.secret_key:
            lack.append("TENCENT_SECRET_KEY")
        if not self.app_id:
            lack.append("call.tencent.voice_sdk_app_id")
        if not self.template_id:
            lack.append("call.tencent.template_id")
        return lack

    @staticmethod
    def _e164(phone: str) -> str:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if phone.strip().startswith("+"):
            return "+" + digits
        return "+86" + digits if len(digits) == 11 else "+" + digits

    def build_payload(self, contact: Contact, slots: dict[str, str]) -> dict[str, Any]:
        values = [slots.get(k, "") for k in self.param_keys] if self.param_keys \
            else list(slots.values())
        return {
            "TemplateId": self.template_id,
            "TemplateParamSet": values,
            "CalledNumber": self._e164(contact.phone),
            "VoiceSdkAppid": self.app_id,
            "PlayTimes": self.play_times,
        }

    def _headers(self, payload: dict[str, Any]) -> dict[str, str]:
        ts = int(time.time())
        date = time.strftime("%Y-%m-%d", time.gmtime(ts))
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        canonical_headers = (f"content-type:application/json; charset=utf-8\n"
                             f"host:{self.host}\n"
                             f"x-tc-action:{self.action.lower()}\n")
        signed_headers = "content-type;host;x-tc-action"
        hashed_body = hashlib.sha256(body.encode()).hexdigest()
        canonical_request = "\n".join(
            ["POST", "/", "", canonical_headers, signed_headers, hashed_body])

        scope = f"{date}/{self.service}/tc3_request"
        to_sign = "\n".join([
            "TC3-HMAC-SHA256", str(ts), scope,
            hashlib.sha256(canonical_request.encode()).hexdigest()])

        def sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = sign(("TC3" + self.secret_key).encode(), date)
        k_service = sign(k_date, self.service)
        k_signing = sign(k_service, "tc3_request")
        signature = hmac.new(k_signing, to_sign.encode(), hashlib.sha256).hexdigest()

        authorization = (f"TC3-HMAC-SHA256 Credential={self.secret_id}/{scope}, "
                         f"SignedHeaders={signed_headers}, Signature={signature}")
        return {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.host,
            "X-TC-Action": self.action,
            "X-TC-Version": self.version,
            "X-TC-Timestamp": str(ts),
            "X-TC-Region": self.region,
        }

    def call(self, contact: Contact, slots: dict[str, str], out_id: str,
             dry_run: bool) -> CallResult:
        payload = self.build_payload(contact, slots)
        base = CallResult(contact_id=contact.id, name=contact.name, phone=contact.phone,
                          role=contact.role, ok=False, provider=self.name,
                          dry_run=dry_run, params=payload)
        if dry_run:
            base.ok = True
            base.code = "DRY_RUN"
            base.message = "未实际拨号（call.dry_run=true）"
            return base
        lack = self.missing()
        if lack:
            base.code = "CONFIG_MISSING"
            base.message = "缺少配置：" + ", ".join(lack)
            return base

        t0 = time.time()
        try:
            resp = _post_json(f"https://{self.host}", self._headers(payload), payload)
            base.latency_ms = int((time.time() - t0) * 1000)
            body = resp.get("Response", {})
            base.request_id = str(body.get("RequestId", ""))
            err = body.get("Error")
            if err:
                base.code = str(err.get("Code", "Error"))
                base.message = str(err.get("Message", ""))
            else:
                send = body.get("SendStatus") or {}
                base.call_id = str(send.get("SessionId", ""))
                base.code = str(send.get("Code", "Ok"))
                base.message = str(send.get("Message", ""))
                base.ok = base.code.lower() in {"ok", "success"}
        except Exception as exc:
            base.latency_ms = int((time.time() - t0) * 1000)
            base.code = "REQUEST_FAILED"
            base.message = f"{type(exc).__name__}: {exc}"
        return base


# ------------------------------------------------------------------ 火山引擎
class VolcVoice:
    """火山引擎语音服务 VMS · 语音通知（SingleBatchAppend）。

    与阿里/腾讯的两点关键差异，接入时最容易踩：

    1. **模板变量用 #name# 而不是 ${name}**。火山的 TTS 模板变量格式是
       井号包裹，且变量名不能含中文、区分大小写。
    2. **幂等由 SingleOpenId 保证**。同一个 OpenId 重复提交不会重复呼叫——
       这正好对应技术文档里"外呼非幂等，按 attempt_id 去重"的要求，
       所以这里用 事件号 + 号码后四位 作为 OpenId，同一事件对同一人
       不会因重试而打两通。

    签名是火山统一的 V4（HMAC-SHA256），与腾讯 TC3 同源但有三处不同：
    派生密钥时 SK 不加前缀（AWS 加 "AWS4"，腾讯加 "TC3"，火山不加）、
    scope 结尾是 "request" 而非 "tc3_request"、必须带 X-Content-Sha256 头。
    """

    name = "volc"
    host = "cloud-vms.volcengineapi.com"
    service = "vms"
    version = "2022-01-01"
    action = "SingleBatchAppend"

    def __init__(self, conf, play_times: int):
        self.access_key = str(conf.get("access_key_id") or "")
        self.secret_key = str(conf.get("secret_access_key") or "")
        self.region = str(conf.get("region") or "cn-north-1")
        self.resource = str(conf.get("tts_resource_key") or "")
        self.number_pool = str(conf.get("number_pool_no") or "")
        self.param_keys = list(conf.get("tts_param_keys") or [])
        self.play_times = play_times

    def missing(self) -> list[str]:
        lack = []
        if not self.access_key:
            lack.append("VOLC_ACCESS_KEY")
        if not self.secret_key:
            lack.append("VOLC_SECRET_KEY")
        if not self.resource:
            lack.append("call.volc.tts_resource_key")
        if not self.number_pool:
            lack.append("call.volc.number_pool_no")
        return lack

    def build_payload(self, contact: Contact, slots: dict[str, str],
                      out_id: str) -> dict[str, Any]:
        keys = self.param_keys or list(slots)
        # 火山的模板变量是 #name# 形式，PhoneParam 传 JSON 字符串
        phone_param = json.dumps({k: slots.get(k, "") for k in keys},
                                 ensure_ascii=False)
        digits = "".join(ch for ch in contact.phone if ch.isdigit())
        return {"List": [{
            "Phone": digits,
            "Type": 1,                       # 1 = TTS 模板
            "Resource": self.resource,
            "NumberPoolNo": self.number_pool,
            # 幂等键：同一事件对同一号码只会呼一次
            "SingleOpenId": f"{out_id}-{digits[-4:]}"[:64],
            "PhoneParam": phone_param,
        }]}

    def _headers(self, body: str) -> dict[str, str]:
        now = time.gmtime()
        x_date = time.strftime("%Y%m%dT%H%M%SZ", now)
        short_date = x_date[:8]
        payload_hash = hashlib.sha256(body.encode()).hexdigest()

        signed_headers = "content-type;host;x-content-sha256;x-date"
        canonical_headers = (
            "content-type:application/json\n"
            f"host:{self.host}\n"
            f"x-content-sha256:{payload_hash}\n"
            f"x-date:{x_date}\n")
        # Action/Version 走 query，必须按 ASCII 序参与签名
        canonical_query = f"Action={self.action}&Version={self.version}"
        canonical_request = "\n".join([
            "POST", "/", canonical_query, canonical_headers,
            signed_headers, payload_hash])

        scope = f"{short_date}/{self.region}/{self.service}/request"
        to_sign = "\n".join([
            "HMAC-SHA256", x_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest()])

        def sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        # 注意：SK 不加任何前缀（AWS 加 AWS4、腾讯加 TC3，火山不加）
        k_date = sign(self.secret_key.encode(), short_date)
        k_region = sign(k_date, self.region)
        k_service = sign(k_region, self.service)
        k_signing = sign(k_service, "request")
        signature = hmac.new(k_signing, to_sign.encode(),
                             hashlib.sha256).hexdigest()

        return {
            "Authorization": (f"HMAC-SHA256 Credential={self.access_key}/{scope}, "
                              f"SignedHeaders={signed_headers}, "
                              f"Signature={signature}"),
            "Content-Type": "application/json",
            "Host": self.host,
            "X-Date": x_date,
            "X-Content-Sha256": payload_hash,
        }

    def call(self, contact: Contact, slots: dict[str, str], out_id: str,
             dry_run: bool) -> CallResult:
        payload = self.build_payload(contact, slots, out_id)
        base = CallResult(contact_id=contact.id, name=contact.name,
                          phone=contact.phone, role=contact.role, ok=False,
                          provider=self.name, dry_run=dry_run, params=payload)
        if dry_run:
            base.ok = True
            base.code = "DRY_RUN"
            base.message = "未实际拨号（call.dry_run=true）"
            return base
        lack = self.missing()
        if lack:
            base.code = "CONFIG_MISSING"
            base.message = "缺少配置：" + ", ".join(lack)
            return base

        t0 = time.time()
        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            url = (f"https://{self.host}/?Action={self.action}"
                   f"&Version={self.version}")
            resp = _post_json(url, self._headers(body), payload)
            base.latency_ms = int((time.time() - t0) * 1000)
            meta = resp.get("ResponseMetadata") or {}
            base.request_id = str(meta.get("RequestId", ""))
            err = meta.get("Error")
            if err:
                base.code = str(err.get("Code", "Error"))
                base.message = str(err.get("Message", ""))
                return base
            result = resp.get("Result") or {}
            # 返回分 SuccessList / FailList，有成功项才算呼出
            success = result.get("SuccessList") or []
            failed = result.get("FailList") or []
            if success:
                base.ok = True
                base.code = "OK"
                base.call_id = str(success[0].get("SingleOpenId", ""))
                base.message = "已提交外呼"
            else:
                base.code = "FAILED"
                base.message = (json.dumps(failed, ensure_ascii=False)[:200]
                                if failed else "返回无成功项")
        except Exception as exc:
            base.latency_ms = int((time.time() - t0) * 1000)
            base.code = "REQUEST_FAILED"
            base.message = f"{type(exc).__name__}: {exc}"
        return base


# -------------------------------------------------------- 推送平台（个人可用）
class SpugVoice:
    """Spug 推送助手的语音通道。**个人账号可用，无需企业资质。**

    为什么需要这个 provider：阿里云、腾讯云、火山引擎的语音通知都卡在
    运营商实名制——必须企业实名认证 + 营业执照 + 话术报备，个人账号
    一概开不了。这不是云厂商的策略而是监管要求，换哪家都一样。

    Spug 这类聚合平台用自己的企业资质承载了合规，个人微信扫码即可用，
    代价是：没有 TTS 模板变量的精细控制（文案由模板在平台侧渲染），
    计费按次且通常比直连云厂商贵，以及多一层服务商依赖。

    对养老告警这个场景这些代价可以接受——它属于"用户主动触发的告警
    通知"，正是这类平台允许的用途；但别拿它做营销外呼，会被封号。

    **模板变量是平台写死的。** Spug 只提供官方固定模板（"您的设备${key1}
    检测到异常${key2}请关注"之类），变量名就是 key1/key2，不能自定义文案。
    所以这里需要一层映射：把系统的四要素（name/time/place/event）拼进
    平台给定的几个变量里。映射由 template_vars 配置，形如
        key1 = "{place}的{name}"
        key2 = "{event}，{time}"

    两种接入方式，按配置自动选：
      template_id 已填 → /send/<模板>，用 targets 动态指定被叫号码
      仅填 user_id    → /xsend/<user_id>，channel=voice，接收人在后台绑定
    """

    name = "spug"
    base = "https://push.spug.cc"

    def __init__(self, conf, play_times: int):
        self.template_id = str(conf.get("template_id") or "").strip()
        self.user_id = str(conf.get("user_id") or "").strip()
        self.channel = str(conf.get("channel") or "voice").strip()
        self.play_times = play_times
        # 兼容两种写法：
        #   template_vars  平台固定变量 → 内容模板（官方模板走这条）
        #   param_keys     直接把同名槽位传过去（自定义模板才可能用上）
        self.template_vars = self._parse_vars(conf.get("template_vars"))
        self.param_keys = list(conf.get("param_keys") or [])

    @staticmethod
    def _parse_vars(raw: Any) -> dict[str, str]:
        """接受 dict，或 ["key1={place}的{name}", ...] 这样的行列表。"""
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        out: dict[str, str] = {}
        for line in (raw or []):
            text = str(line).strip()
            if not text or "=" not in text:
                continue
            key, _, tpl = text.partition("=")
            key = key.strip()
            if key:
                out[key] = tpl.strip()
        return out

    def missing(self) -> list[str]:
        lack = []
        if not self.template_id and not self.user_id:
            lack.append("call.spug.template_id 或 call.spug.user_id（至少填一个）")
        if self.template_id and not self.template_vars and not self.param_keys:
            lack.append("call.spug.template_vars（官方模板的 key1/key2 填什么）")
        return lack

    def _render(self, slots: dict[str, str]) -> dict[str, str]:
        """按 template_vars 把四要素拼进平台固定变量。"""
        out: dict[str, str] = {}
        for key, tpl in self.template_vars.items():
            try:
                out[key] = tpl.format(**slots)
            except (KeyError, IndexError):
                # 模板里写了系统没有的槽位：原样传过去而不是整条失败，
                # 护工听到一句带占位符的话，好过完全没接到电话
                out[key] = tpl
        return out

    def _text(self, slots: dict[str, str]) -> str:
        """四要素短句，给 /xsend 的自由文案通道用。"""
        return (f"{slots.get('place', '')}的{slots.get('name', '')}"
                f"于{slots.get('time', '')}检测到{slots.get('event', '异常')}，"
                f"请立即前往查看。")

    def build_request(self, contact: Contact, slots: dict[str, str],
                      out_id: str) -> tuple[str, dict[str, Any]]:
        digits = "".join(ch for ch in contact.phone if ch.isdigit())
        if self.template_id:
            body: dict[str, Any] = dict(self._render(slots))
            for key in self.param_keys:          # 自定义模板的直传槽位
                body.setdefault(key, slots.get(key, ""))
            # targets 会覆盖模板里配置的接收人，这样同一个模板能按
            # 升级链呼不同的人，不必在平台后台为每个联系人建一个模板
            body["targets"] = digits
            return f"{self.base}/send/{self.template_id}", body
        return f"{self.base}/xsend/{self.user_id}", {
            "title": f"{slots.get('event', '异常')}预警",
            "content": self._text(slots),
            "channel": self.channel,
        }

    def call(self, contact: Contact, slots: dict[str, str], out_id: str,
             dry_run: bool) -> CallResult:
        url, body = self.build_request(contact, slots, out_id)
        shown = dict(body)
        if "targets" in shown:
            shown["targets"] = _mask(str(shown["targets"]))
        base = CallResult(contact_id=contact.id, name=contact.name,
                          phone=contact.phone, role=contact.role, ok=False,
                          provider=self.name, dry_run=dry_run,
                          params={"url": url, "body": shown})
        if dry_run:
            base.ok = True
            base.code = "DRY_RUN"
            base.message = "未实际拨号（call.dry_run=true）"
            return base
        lack = self.missing()
        if lack:
            base.code = "CONFIG_MISSING"
            base.message = "缺少配置：" + ", ".join(lack)
            return base

        t0 = time.time()
        try:
            resp = _post_json(url, {"Content-Type": "application/json"}, body)
            base.latency_ms = int((time.time() - t0) * 1000)
            base.request_id = str(resp.get("request_id", ""))
            code = resp.get("code")
            base.ok = code == 200
            if not base.ok:
                base.code = str(code)
                base.message = self._explain(base.code, str(resp.get("msg", "")))
            else:
                # **平台返回 200 只代表"请求已受理"，不代表电话打通了。**
                # Spug 的 /send 把请求收进队列就返回成功；真正的拨号结果
                # （余额不足、模板通道不是语音、号码被拦截）只在控制台的
                # 发送记录里看得到，接口不回传。
                #
                # 所以这里不写"成功"。这是生命安全链路的审计记录，
                # 声称打过电话而实际没打通，比如实说"只是提交了"危险得多——
                # 事后复盘时没人会去质疑一条写着"成功"的记录。
                base.code = "SUBMITTED"
                base.message = (
                    f"已提交推送平台，未确认接通（request_id={base.request_id}）。"
                    f"若未响铃，在 Spug 控制台「发送记录」按此号核对："
                    f"常见原因是语音次数/余额不足、模板通道不是语音、"
                    f"或模板未配置推送对象导致 targets 无处覆盖")
        except Exception as exc:
            base.latency_ms = int((time.time() - t0) * 1000)
            base.code = "REQUEST_FAILED"
            base.message = f"{type(exc).__name__}: {exc}"
        return base

    @staticmethod
    def _explain(code: str, msg: str) -> str:
        """给平台返回的错误补一句"该怎么办"。

        原始消息只说"IP 不在白名单"，不说去哪儿加——值班的人看到日志
        要能直接动手，不用回来查文档。
        """
        hints = {
            "白名单": "：去 push.spug.cc 的「个人设置 → IP 白名单」添加上述 IP。"
                      "家用宽带 IP 会变，换网络后需重新添加",
            "余额": "：Spug 语音按次计费，去控制台充值",
            "模板": "：核对模板编码是否正确、模板是否已启用且通道为「电话」",
            "频率": "：触发平台频率限制，稍后重试",
            "参数": "：核对「变量填充」里的 key 名与 Spug 模板中的 ${} 是否一致",
        }
        for key, hint in hints.items():
            if key in msg:
                return msg + hint
        return msg


class NoopVoice:
    name = "none"

    def __init__(self, *_args, **_kwargs):
        pass

    def missing(self) -> list[str]:
        return ["call.provider 未配置"]

    def call(self, contact: Contact, slots: dict[str, str], out_id: str,
             dry_run: bool) -> CallResult:
        return CallResult(contact_id=contact.id, name=contact.name, phone=contact.phone,
                          role=contact.role, ok=False, provider=self.name,
                          dry_run=True, code="NO_PROVIDER",
                          message="未配置外呼通道，仅记录")


class CallGateway:
    """按联系人顺序外呼，支持呼通即停或全部呼叫。"""

    def __init__(self, cfg):
        c = cfg.call
        self.provider_name = str(c.provider or "none").lower()
        self.dry_run = bool(c.dry_run)
        self.max_calls = int(c.max_calls_per_event)
        self.call_all = bool(c.call_all_contacts)
        self.contacts = [Contact.from_dict(raw) for raw in (c.get("contacts") or [])]
        self.contacts = [ct for ct in self.contacts if ct.phone]

        play_times = int(c.play_times)
        builders = {
            "aliyun": lambda: AliyunVoice(c.aliyun, play_times),
            "tencent": lambda: TencentVoice(c.tencent, play_times),
            "volc": lambda: VolcVoice(c.get("volc") or {}, play_times),
            "spug": lambda: SpugVoice(c.get("spug") or {}, play_times),
        }
        build = builders.get(self.provider_name)
        self.provider: Any = build() if build else NoopVoice()

    def describe(self) -> str:
        mode = "演练(dry_run)" if self.dry_run else "真实拨号"
        lack = self.provider.missing()
        suffix = f"，待补配置：{', '.join(lack)}" if lack and not self.dry_run else ""
        return (f"{self.provider_name} / {mode} / "
                f"{len(self.contacts)} 位联系人{suffix}")

    def contact_by_id(self, contact_id: str) -> Contact | None:
        return next((c for c in self.contacts if c.id == contact_id), None)

    def call_one(self, contact: Contact, slots: dict[str, str],
                 out_id: str) -> CallResult:
        return self.provider.call(contact, slots, out_id, self.dry_run)

    def call_sequence(self, slots: dict[str, str], out_id: str,
                      roles: list[str] | None = None) -> list[CallResult]:
        targets = self.contacts
        if roles:
            targets = [c for c in targets if c.role in roles] or targets
        results: list[CallResult] = []
        for contact in targets[: self.max_calls]:
            res = self.call_one(contact, slots, out_id)
            results.append(res)
            if res.ok and not self.call_all:
                break
        return results
