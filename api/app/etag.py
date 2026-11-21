"""整日观测查询的 ETag 工具.

ETag 由数据库中的站日版本号 (observation_day_versions.version) 派生:
版本号在每次触及该站日的已提交写入后单调递增, 因此标签与已提交状态一一对应,
且持久化在库内——重启后旧标签仍然有效.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional


def make_etag(version: int) -> str:
    """强验证器, 不透明字符串; 客户端应原样回传, 不应解析其中的数字."""
    return f'"obs-v{version}"'


def whole_utc_day(from_: Optional[datetime], to: Optional[datetime]) -> Optional[date]:
    """from/to 恰好圈定一个 UTC 日 [00:00, 次日 00:00) 时返回该 UTC 日, 否则 None."""
    if from_ is None or to is None:
        return None
    f = from_.astimezone(timezone.utc)
    t = to.astimezone(timezone.utc)
    midnight = (0, 0, 0, 0)
    if (f.hour, f.minute, f.second, f.microsecond) != midnight:
        return None
    if (t.hour, t.minute, t.second, t.microsecond) != midnight:
        return None
    if t != f + timedelta(days=1):
        return None
    return f.date()


def if_none_match_values(header: Optional[str]) -> set[str]:
    """解析 If-None-Match 为标签集合; 去掉 W/ 弱比较前缀, 保留 '*' 通配."""
    if not header:
        return set()
    values: set[str] = set()
    for part in header.split(","):
        token = part.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token:
            values.add(token)
    return values


def etag_matches(etag: str, if_none_match: Optional[str]) -> bool:
    """If-None-Match 采用弱比较; '*' 匹配任意资源."""
    values = if_none_match_values(if_none_match)
    return "*" in values or etag in values
