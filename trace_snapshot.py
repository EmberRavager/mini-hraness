"""调用记录的 JSON 展示副本。按常见字段和文本格式有限脱敏，无法识别任意正文中的秘密。

仅处理调用方传入的值，不采集 HTTP headers 或环境变量。结果不能用于重放调用。
"""
import json
import math
import re


_REDACTED = "[redacted]"
_CUT = "[truncated]"
_PRIVATE = re.compile(r"(?:think(?:ing)?|reasoning).*|(?:request|response)?headers|env(?:ironment)?")
_MEDIA_FIELDS = {"image", "images", "imageurl", "audio", "inputaudio", "video", "base64",
                 "imagedata", "audiodata", "videodata"}
_CREDENTIAL = (r"api[_-]?key|(?:access|secret)[_-]?key(?:[_-]?(?:id|secret))?|private[_-]?key|"
               r"(?:access[_-]?|refresh[_-]?|auth[_-]?|id[_-]?)?token|passw(?:or)?d|"
               r"(?:client[_-]?)?secret|authorization|cookie")
_SECRET_KEY = re.compile(r"(?:" + _CREDENTIAL + r")$", re.I)
_ASSIGNMENT = re.compile(r"""(?i)(\b(?:%s)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'[^']*'|[^\s,;&}]+)"""
                         % _CREDENTIAL)
_THOUGHT = re.compile(r"<(think|thinking|reasoning)>(?:.*?</\1\s*>|.*\Z)", re.I | re.S)
_MEDIA = re.compile(r"data:[\w.+/-]*;base64,[A-Za-z0-9+/=_%-]*|"
                    r"(?:iVBORw0KGgo|/9j/|R0lGOD|UklGR|SUQz)[A-Za-z0-9+/=]+", re.I)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_TOKEN = re.compile(r"\b(?:sk-[\w-]{8,}|gh[pousr]_\w{8,}|github_pat_\w{8,}|AKIA[A-Z0-9]{16})")
_PEM = re.compile(r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?(?:-----END \1-----|\Z)", re.S)
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I)


def _clean_text(text):
    text = _THOUGHT.sub("", text)
    text = _MEDIA.sub(_REDACTED, text)
    text = _PEM.sub(_REDACTED, text)
    text = _USERINFO.sub(r"\1" + _REDACTED + "@", text)
    text = _AUTH.sub(r"\1 " + _REDACTED, text)
    text = _ASSIGNMENT.sub(r"\1" + _REDACTED, text)
    return _TOKEN.sub(_REDACTED, text)


def snapshot(value):
    """复制 JSON 值；限制字符串、集合、嵌套深度及单次快照的总展示量。"""
    remaining = 32768

    def clip(clean):
        nonlocal remaining
        limit = min(4096, max(remaining, 0))
        remaining -= min(len(clean), limit)
        return clean if len(clean) <= limit else clean[:limit] + _CUT

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if depth >= 24 or remaining <= 0:
            return _CUT
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index == 128 or remaining <= 0:
                    result[_CUT] = True
                    break
                key = str(key)
                field = re.sub(r"[^a-z0-9]", "", key.lower())
                if _PRIVATE.fullmatch(field):
                    continue
                public_key = clip(_clean_text(key))
                encoded_data = field == "data" and (
                    item.get("type") == "base64" or item.get("encoding") == "base64"
                    or any(k in item for k in ("mimeType", "mime_type", "media_type")))
                if encoded_data or field in _MEDIA_FIELDS or field.endswith("base64") or _SECRET_KEY.search(field):
                    result[public_key] = _REDACTED
                else:
                    result[public_key] = visit(child, depth + 1)
            return result
        if isinstance(item, (tuple, list)):
            result = []
            for index, child in enumerate(item):
                if index == 128 or remaining <= 0:
                    result.append(_CUT)
                    break
                result.append(visit(child, depth + 1))
            return result
        if isinstance(item, str):
            # Tool results and legacy feedback can contain a JSON document inside message.content.
            prefix, sep, tail = item.partition("\n")
            embedded = item.lstrip()
            if not embedded.startswith(("{", "[")) and sep:
                embedded = tail.lstrip()
            else:
                prefix = sep = ""
            if embedded.startswith(("{", "[")):
                try:
                    parsed = json.loads(embedded)
                except (ValueError, RecursionError):
                    pass
                else:
                    public = visit(parsed, depth + 1)
                    if public != parsed:
                        return clip(_clean_text(prefix) + sep + json.dumps(public, ensure_ascii=False))
            return clip(_clean_text(item))
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        return "[unsupported]"

    return visit(value, 0)
