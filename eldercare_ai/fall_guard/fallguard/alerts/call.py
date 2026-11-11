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
        if self.provider_name == "aliyun":
            self.provider: Any = AliyunVoice(c.aliyun, play_times)
        elif self.provider_name == "tencent":
            self.provider = TencentVoice(c.tencent, play_times)
        else:
            self.provider = NoopVoice()

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
