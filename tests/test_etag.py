"""ETag/条件请求集成测试: 需要运行中的 API 与数据库.

覆盖:
  * 整日查询发 ETag, If-None-Match 命中 -> 304(不查明细), 未命中 -> 200 新标签
  * 观测/时段的 API 与直连 SQL 增删改后, 新旧站日换标签; 删空当天同样换
  * 归属时段变更只刷新覆盖到观测的站日; 回滚后原标签仍有效
  * 标签持久化(库表), 重启后可继续使用(见 scripts/etag_restart_check.sh)
  * 非整日查询不发 ETag
"""
from __future__ import annotations

import os
import uuid

import httpx
import psycopg
import pytest

API = os.environ.get("API_URL", "http://localhost:8000")
DSN = os.environ.get("DATABASE_URL", "postgresql://watershed:watershed@localhost:5432/watershed")

DAY1 = "2021-06-15"
DAY1_FROM = f"{DAY1}T00:00:00Z"
DAY1_TO = "2021-06-16T00:00:00Z"
DAY2_FROM = "2021-06-16T00:00:00Z"
DAY2_TO = "2021-06-17T00:00:00Z"
DAY3_FROM = "2021-06-17T00:00:00Z"
DAY3_TO = "2021-06-18T00:00:00Z"


@pytest.fixture()
def client():
    with httpx.Client(base_url=API, timeout=30) as c:
        yield c


def new_sid() -> str:
    return "ET-" + uuid.uuid4().hex[:12]


def mk_station_with_day(client: httpx.Client, sid: str, period_to: str | None = "2022-01-01T00:00:00Z") -> None:
    r = client.post("/stations", json={"station_id": sid, "name": "etag-pytest"})
    assert r.status_code == 201, r.text
    period = {
        "op": "insert_period", "station_id": sid, "watershed": "WS-A",
        "valid_from": "2020-01-01T00:00:00Z", "valid_to": period_to,
    }
    r = client.post("/changesets", json={"operations": [period]})
    assert r.status_code == 201, r.text


def day_get(client: httpx.Client, sid: str, d_from: str = DAY1_FROM, d_to: str = DAY1_TO,
            if_none_match: str | None = None) -> httpx.Response:
    headers = {"If-None-Match": if_none_match} if if_none_match is not None else {}
    return client.get(f"/stations/{sid}/observations",
                      params={"from": d_from, "to": d_to}, headers=headers)


def etag_of(client: httpx.Client, sid: str, d_from: str = DAY1_FROM, d_to: str = DAY1_TO) -> str:
    r = day_get(client, sid, d_from, d_to)
    assert r.status_code == 200, r.text
    assert r.headers.get("ETag")
    return r.headers["ETag"]


# ---------------------------------------------------------------------------
# 基础: 标签发放与 304
# ---------------------------------------------------------------------------

def test_non_whole_day_query_has_no_etag(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    # 缺参数 / 跨度不是一天 / 起点不在 UTC 午夜, 都不发 ETag
    r = client.get(f"/stations/{sid}/observations")
    assert r.status_code == 200 and "ETag" not in r.headers
    r = client.get(f"/stations/{sid}/observations",
                   params={"from": DAY1_FROM, "to": "2021-06-17T00:00:00Z"})
    assert r.status_code == 200 and "ETag" not in r.headers
    r = client.get(f"/stations/{sid}/observations",
                   params={"from": "2021-06-15T01:00:00Z", "to": DAY1_TO})
    assert r.status_code == 200 and "ETag" not in r.headers


def test_whole_day_first_request_200_then_304_saves_detail_query(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    r = client.post("/observations", json={
        "station_id": sid, "observed_at": f"{DAY1}T08:00:00Z", "value": 1.5})
    assert r.status_code == 201, r.text

    r1 = day_get(client, sid)
    assert r1.status_code == 200
    tag = r1.headers["ETag"]
    assert tag.startswith('"') and tag.endswith('"')
    assert len(r1.json()) == 1

    # 带回旧标签: 304, 无响应体, 标签原样返回
    r2 = day_get(client, sid, if_none_match=tag)
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers["ETag"] == tag

    # 带错标签: 200 带明细和当前标签
    r3 = day_get(client, sid, if_none_match='"obs-v99999"')
    assert r3.status_code == 200
    assert r3.headers["ETag"] == tag
    assert len(r3.json()) == 1


def test_if_none_match_variants(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    tag = etag_of(client, sid)
    # 弱前缀 W/ 按弱比较命中
    r = day_get(client, sid, if_none_match=f"W/{tag}")
    assert r.status_code == 304
    # 通配 * 命中
    r = day_get(client, sid, if_none_match="*")
    assert r.status_code == 304
    # 多标签列表中任一命中
    r = day_get(client, sid, if_none_match=f'"obs-v1", {tag}')
    assert r.status_code == 304


def test_empty_day_also_has_stable_etag(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    tag = etag_of(client, sid)
    r = day_get(client, sid, if_none_match=tag)
    assert r.status_code == 304
    # 空日重复查询标签不变
    assert etag_of(client, sid) == tag


# ---------------------------------------------------------------------------
# 观测写入换标签: 新旧站日
# ---------------------------------------------------------------------------

def test_observation_insert_update_delete_bumps_new_and_old_days(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    tag1 = etag_of(client, sid)
    tag2_before = etag_of(client, sid, DAY2_FROM, DAY2_TO)

    # 补录到 DAY1 -> DAY1 换标签, DAY2 不变
    r = client.post("/observations", json={
        "station_id": sid, "observed_at": f"{DAY1}T08:00:00Z", "value": 1.0})
    assert r.status_code == 201
    tag1_after = etag_of(client, sid)
    assert tag1_after != tag1
    assert etag_of(client, sid, DAY2_FROM, DAY2_TO) == tag2_before

    # 304 旧标签失效 -> 200
    assert day_get(client, sid, if_none_match=tag1).status_code == 200
    assert day_get(client, sid, if_none_match=tag1_after).status_code == 304

    # 改采样时刻 DAY1 -> DAY2: 新旧两个站日都换标签
    obs_id = day_get(client, sid).json()[0]["observation_id"]
    r = client.post("/changesets", json={"operations": [
        {"op": "update_observation", "observation_id": obs_id,
         "observed_at": "2021-06-16T09:00:00Z"}]})
    assert r.status_code == 201, r.text
    assert etag_of(client, sid) != tag1_after
    tag2_after = etag_of(client, sid, DAY2_FROM, DAY2_TO)
    assert tag2_after != tag2_before

    # 删除: 所在站日再换一次, DAY1 不受影响
    r = client.post("/changesets", json={"operations": [
        {"op": "delete_observation", "observation_id": obs_id}]})
    assert r.status_code == 201, r.text
    assert etag_of(client, sid, DAY2_FROM, DAY2_TO) != tag2_after


def test_delete_all_observations_still_bumps_tag(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    client.post("/observations", json={
        "station_id": sid, "observed_at": f"{DAY1}T08:00:00Z", "value": 1.0})
    tag_with_data = etag_of(client, sid)
    obs_id = day_get(client, sid).json()[0]["observation_id"]

    r = client.post("/changesets", json={"operations": [
        {"op": "delete_observation", "observation_id": obs_id}]})
    assert r.status_code == 201, r.text

    # 删空当天: 标签变化, 但空日仍有稳定标签且可 304
    tag_empty = etag_of(client, sid)
    assert tag_empty != tag_with_data
    assert day_get(client, sid, if_none_match=tag_empty).status_code == 304
    assert day_get(client, sid).json() == []


def test_observation_move_between_stations_bumps_both(client):
    s1, s2 = new_sid(), new_sid()
    mk_station_with_day(client, s1)
    mk_station_with_day(client, s2)
    client.post("/observations", json={
        "station_id": s1, "observed_at": f"{DAY1}T08:00:00Z", "value": 1.0})
    t1, t2 = etag_of(client, s1), etag_of(client, s2)
    obs_id = day_get(client, s1).json()[0]["observation_id"]

    r = client.post("/changesets", json={"operations": [
        {"op": "update_observation", "observation_id": obs_id, "station_id": s2}]})
    assert r.status_code == 201, r.text
    assert etag_of(client, s1) != t1
    assert etag_of(client, s2) != t2


# ---------------------------------------------------------------------------
# 时段写入: 只刷新覆盖到观测的站日
# ---------------------------------------------------------------------------

def test_period_change_bumps_only_days_with_observations(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    # DAY1 有观测, DAY3 无观测
    client.post("/observations", json={
        "station_id": sid, "observed_at": f"{DAY1}T08:00:00Z", "value": 1.0})
    t1 = etag_of(client, sid)
    t2 = etag_of(client, sid, DAY2_FROM, DAY2_TO)
    t3 = etag_of(client, sid, DAY3_FROM, DAY3_TO)

    # 仅改流域: 覆盖范围不变, 但供数 watershed 会变 -> 有观测的 DAY1 换,
    # 无观测的 DAY3 不产生版本变化(版本行是查询时懒建的, 标签保持 v1)
    pid = client.get(f"/stations/{sid}/periods").json()[0]["period_id"]
    r = client.post("/changesets", json={"operations": [
        {"op": "update_period", "period_id": pid, "watershed": "WS-RENAMED"}]})
    assert r.status_code == 201, r.text
    assert etag_of(client, sid) != t1
    assert etag_of(client, sid, DAY2_FROM, DAY2_TO) == t2
    assert etag_of(client, sid, DAY3_FROM, DAY3_TO) == t3

    # 扩大时段范围到更早: 并集范围仍覆盖 DAY1 的观测 -> DAY1 再刷一次;
    # 没有观测的 DAY2/DAY3 始终不受影响
    t1b = etag_of(client, sid)
    r = client.post("/changesets", json={"operations": [
        {"op": "update_period", "period_id": pid, "valid_from": "2019-01-01T00:00:00Z"}]})
    assert r.status_code == 201, r.text
    assert etag_of(client, sid) != t1b
    assert etag_of(client, sid, DAY2_FROM, DAY2_TO) == t2
    assert etag_of(client, sid, DAY3_FROM, DAY3_TO) == t3


def test_transfer_bumps_attributed_day(client):
    sid = new_sid()
    # 至今有效的时段, 便于 transfer
    mk_station_with_day(client, sid, period_to=None)
    client.post("/observations", json={
        "station_id": sid, "observed_at": f"{DAY1}T08:00:00Z", "value": 1.0})
    tag = etag_of(client, sid)

    r = client.post(f"/stations/{sid}/transfer",
                    json={"at": "2021-01-01T00:00:00Z", "new_watershed": "WS-B"})
    assert r.status_code == 201, r.text
    # 转属后该日供数流域变化, 标签必须换
    new_tag = etag_of(client, sid)
    assert new_tag != tag
    assert day_get(client, sid).json()[0]["watershed"] == "WS-B"
    assert day_get(client, sid, if_none_match=tag).status_code == 200
    assert day_get(client, sid, if_none_match=new_tag).status_code == 304


# ---------------------------------------------------------------------------
# 直连 SQL 写入同样换标签; 回滚后原标签仍有效
# ---------------------------------------------------------------------------

def test_direct_sql_writes_bump_tags_and_rollback_keeps_tag(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    tag = etag_of(client, sid)

    # 直连 SQL 补录 -> 标签换
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(
            "INSERT INTO observations (station_id, observed_at, value)"
            " VALUES (%s, %s, %s)",
            (sid, f"{DAY1}T10:00:00+00", 3.3))
    new_tag = etag_of(client, sid)
    assert new_tag != tag

    # 直连 SQL 失败事务(无归属补录): 整笔回滚, 标签保持
    with psycopg.connect(DSN) as c:
        c.execute(
            "INSERT INTO observations (station_id, observed_at, value)"
            " VALUES (%s, %s, %s)",
            (sid, "2025-01-01T00:00:00+00", 9.9))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.commit()
    assert etag_of(client, sid) == new_tag
    assert day_get(client, sid, if_none_match=new_tag).status_code == 304

    # 直连 SQL 改观测值(同日) -> 标签换; 再验证时段触发器也对直连 SQL 生效
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("UPDATE observations SET value = 4.4 WHERE station_id = %s", (sid,))
        c.execute("UPDATE attribution_periods SET watershed = 'WS-SQL' "
                  "WHERE station_id = %s", (sid,))
    sql_tag = etag_of(client, sid)
    assert sql_tag != new_tag
    assert day_get(client, sid, if_none_match=sql_tag).status_code == 304


def test_rolled_back_api_changeset_keeps_tag(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    client.post("/observations", json={
        "station_id": sid, "observed_at": f"{DAY1}T08:00:00Z", "value": 1.0})
    tag = etag_of(client, sid)

    # 违规变更集(重叠时段)整笔回滚
    r = client.post("/changesets", json={"operations": [
        {"op": "insert_period", "station_id": sid, "watershed": "WS-X",
         "valid_from": "2020-06-01T00:00:00Z", "valid_to": "2022-01-01T00:00:00Z"}]})
    assert r.status_code == 409, r.text
    assert etag_of(client, sid) == tag
    assert day_get(client, sid, if_none_match=tag).status_code == 304


# ---------------------------------------------------------------------------
# 标签与数据对应同一已提交状态
# ---------------------------------------------------------------------------

def test_etag_always_matches_returned_payload(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    for i in range(3):
        client.post("/observations", json={
            "station_id": sid, "observed_at": f"{DAY1}T0{i}:00:00Z", "value": float(i)})
        r = day_get(client, sid)
        assert r.status_code == 200
        tag = r.headers["ETag"]
        payload = r.json()
        assert len(payload) == i + 1
        # 同标签立刻条件请求必须 304
        assert day_get(client, sid, if_none_match=tag).status_code == 304


def test_timezone_offsets_normalized_to_utc_day(client):
    sid = new_sid()
    mk_station_with_day(client, sid)
    # +08:00 的 08:00 恰为 UTC 午夜, 到次日 UTC 午夜 = 同一个 UTC 日
    r = day_get(client, sid, "2021-06-15T08:00:00+08:00", "2021-06-16T08:00:00+08:00")
    assert r.status_code == 200
    assert "ETag" in r.headers
    # 与 UTC 表示查到的是同一个站日、同一个标签
    assert r.headers["ETag"] == etag_of(client, sid)
