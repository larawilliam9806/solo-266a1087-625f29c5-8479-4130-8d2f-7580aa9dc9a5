import io
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "t"}
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, kind, csv_text, filename="data.csv"):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    return client.post(
        f"/upload/{kind}", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def usage(client, point, start, end):
    resp = client.get(f"/api/usage?point={point}&start={start}&end={end}")
    assert resp.status_code == 200
    return resp.get_json()


def day_kwh(result, day):
    return next(d["kwh"] for d in result["days"] if d["day"] == day)


# ---------------------------------------------------------------- 基本规则

def test_basic_daily_usage(client):
    csv_text = (
        "point,ts,value\n"
        "A,2026-01-01T00:00:00Z,100\n"
        "A,2026-01-02T00:00:00Z,150\n"
        "A,2026-01-03T00:00:00Z,230\n"
    )
    resp = upload(client, "readings", csv_text)
    assert resp.status_code == 200
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-01") == "0"      # 首条读数无前一读数，不计增量
    assert day_kwh(r, "2026-01-02") == "50"     # 增量计入后次读数所属 UTC 日
    assert day_kwh(r, "2026-01-03") == "80"
    assert r["total_kwh"] == "130"
    assert r["gaps"] == []


def test_decimal_precision(client):
    csv_text = (
        "point,ts,value\n"
        "A,2026-01-01T00:00:00Z,0.1\n"
        "A,2026-01-02T00:00:00Z,0.3\n"
    )
    upload(client, "readings", csv_text)
    r = usage(client, "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "0.2"    # 十进制精确，不是 0.19999...


def test_idempotent_reupload(client):
    csv_text = (
        "point,ts,value\n"
        "A,2026-01-01T00:00:00Z,100\n"
        "A,2026-01-02T00:00:00Z,150\n"
    )
    upload(client, "readings", csv_text)
    resp = upload(client, "readings", csv_text)  # 原样重传
    assert "重复跳过 2 条" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "50"      # 不重复计数


def test_conflict_same_ts_different_value_rejected(client):
    upload(client, "readings", "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                               "A,2026-01-02T00:00:00Z,150\n")
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-02T00:00:00Z,151\n"
                  "A,2026-01-03T00:00:00Z,200\n")
    body = resp.get_data(as_text=True)
    assert "已存在不同读数" in body and "整批拒绝" in body
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "50"      # 旧数据保持不变
    assert day_kwh(r, "2026-01-03") == "0"       # 同批其它行也未生效


def test_conflict_within_file_rejected(client):
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                  "A,2026-01-01T00:00:00Z,101\n")
    assert "同一计量点同一时刻出现不同读数" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-01")
    assert r["total_kwh"] == "0"


def test_negative_delta_rejected_and_atomic(client):
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                  "A,2026-01-02T00:00:00Z,90\n")   # 倒退
    body = resp.get_data(as_text=True)
    assert "读数倒退" in body and "整批拒绝" in body
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["total_kwh"] == "0"                  # 整批未生效


def test_bad_row_makes_whole_batch_fail(client):
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                  "A,not-a-time,150\n")
    assert "上传被拒绝" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-01")
    assert r["total_kwh"] == "0"


def test_negative_value_and_naive_ts_rejected(client):
    assert "不能为负" in upload(
        client, "readings", "point,ts,value\nA,2026-01-01T00:00:00Z,-1\n"
    ).get_data(as_text=True)
    assert "UTC" in upload(
        client, "readings", "point,ts,value\nA,2026-01-01 08:00:00,1\n"
    ).get_data(as_text=True)
    assert "UTC" in upload(
        client, "readings", "point,ts,value\nA,2026-01-01T08:00:00+08:00,1\n"
    ).get_data(as_text=True)


# ---------------------------------------------------------------- 换表与缺口

def test_swap_creates_gap_and_revokes_delta(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n"
           "A,2026-01-03T00:00:00Z,170\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "50"

    # 补录换表（发生在后次读数时刻）→ 原计入的 50 撤销，区间变缺口
    upload(client, "swaps", "point,ts\nA,2026-01-02T00:00:00Z\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "0"
    assert day_kwh(r, "2026-01-03") == "20"      # 换表后的区间仍正常计量
    assert len(r["gaps"]) == 1
    assert r["gaps"][0]["start"] == "2026-01-01T00:00:00.000000Z"
    assert r["gaps"][0]["end"] == "2026-01-02T00:00:00.000000Z"
    gap_day = next(d for d in r["days"] if d["day"] == "2026-01-02")
    assert gap_day["gap"] is True


def test_swap_between_readings_marks_gap(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,180\n")
    upload(client, "swaps", "point,ts\nA,2026-01-02T12:00:00Z\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert r["total_kwh"] == "0"                  # 换表区间不推算增量
    assert len(r["gaps"]) == 1


def test_swap_at_earlier_reading_does_not_gap_following_interval(client):
    # 换表时刻 == 前次读数时刻：属于上一个区间（含后次时刻），不影响后一区间
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    upload(client, "swaps", "point,ts\nA,2026-01-01T00:00:00Z\n")
    r = usage(client, "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "50"


def test_swap_upload_idempotent(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    upload(client, "swaps", "point,ts\nA,2026-01-02T00:00:00Z\n")
    resp = upload(client, "swaps", "point,ts\nA,2026-01-02T00:00:00Z\n")
    assert "重复跳过 1 条" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert len(r["gaps"]) == 1


# ---------------------------------------------------------------- 晚到读数

def test_late_reading_splits_interval(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-03") == "60"

    # 晚到读数拆分旧区间：60 拆成 30 + 30
    upload(client, "readings", "point,ts,value\nA,2026-01-02T00:00:00Z,130\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-02") == "30"
    assert day_kwh(r, "2026-01-03") == "30"
    assert r["total_kwh"] == "60"


def test_late_reading_causing_backwards_rejected(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-03T00:00:00Z,160\n")
    resp = upload(client, "readings", "point,ts,value\nA,2026-01-02T00:00:00Z,200\n")
    assert "读数倒退" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-03") == "60"       # 旧数据保持


# ---------------------------------------------------------------- 页面

def test_pages_render(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    assert client.get("/").status_code == 200
    resp = client.get("/usage?point=A&start=2026-01-01&end=2026-01-02")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "50" in body
    # 默认日期范围（不填日期）
    assert client.get("/usage?point=A").status_code == 200


# ---------------------------------------------------------------- 并发一致性

def test_concurrent_uploads_and_queries(app):
    """并发上传 + 查询：每次查询看到的都是某一版的完整结果。"""
    points = [f"P{i}" for i in range(4)]
    days = [f"2026-01-{d:02d}" for d in range(1, 11)]

    def csv_for(point):
        lines = ["point,ts,value"]
        for i, day in enumerate(days):
            lines.append(f"{point},{day}T00:00:00Z,{1000 + i * 10}")
        return "\n".join(lines) + "\n"

    errors = []

    def do_upload(point):
        c = app.test_client()
        try:
            resp = upload(c, "readings", csv_for(point))
            if resp.status_code != 200:
                errors.append(f"upload {point}: {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"upload {point}: {e}")

    def do_query():
        c = app.test_client()
        expected_full = str(sum(10 for _ in range(9)))  # 90
        for _ in range(30):
            for p in points:
                try:
                    r = usage(c, p, "2026-01-01", "2026-01-10")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"query {p}: {e}")
                    return
                # 单批原子性：要么整批未生效(0)，要么整批完整生效(90)
                if r["total_kwh"] not in ("0", expected_full):
                    errors.append(f"query {p}: 看到部分结果 {r['total_kwh']}")
                    return

    threads = ([threading.Thread(target=do_upload, args=(p,)) for p in points]
               + [threading.Thread(target=do_query) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    c = app.test_client()
    for p in points:
        r = usage(c, p, "2026-01-01", "2026-01-10")
        assert r["total_kwh"] == "90"


def test_concurrent_identical_uploads_idempotent(app):
    csv_text = ("point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
                "A,2026-01-02T00:00:00Z,150\n")
    errors = []

    def do_upload():
        c = app.test_client()
        try:
            resp = upload(c, "readings", csv_text)
            if resp.status_code != 200:
                errors.append(resp.status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    threads = [threading.Thread(target=do_upload) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    r = usage(app.test_client(), "A", "2026-01-02", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "50"  # 并发重传不重复计数


# ---------------------------------------------------------------- 补抄单
from datetime import datetime, timedelta, timezone  # noqa: E402


def closed_day(days_ago=1):
    return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).strftime(
        "%Y-%m-%d"
    )


def open_order(client, point, day):
    return client.post(
        "/supplement/open", data={"point": point, "day": day}, follow_redirects=True
    )


def order_page(client, order_id):
    resp = client.get(f"/supplement/{order_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def upload_order(client, order_id, csv_text):
    data = {
        "file": (io.BytesIO(csv_text.encode("utf-8")), "sup.csv"),
        "order_id": str(order_id),
    }
    return client.post(
        "/upload/supplement", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def seed_hourly(client, point, day, values):
    """上传某日若干整点读数，values 形如 {'00': 100, '01': 110}。"""
    lines = ["point,ts,value"]
    for hh, v in values.items():
        lines.append(f"{point},{day}T{hh}:00:00Z,{v}")
    upload(client, "readings", "\n".join(lines) + "\n")


def test_open_order_snapshots_missing_hours_and_dedups(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100, "01": 110})
    resp = open_order(client, "A", day)
    assert "已开补抄单" in resp.get_data(as_text=True)
    body = order_page(client, 1)
    # 00、01 已有读数，不在单上；其余 22 个整点待补
    assert "仍待补 22 个" in body
    assert body.count("待补") >= 22

    # 同一点同一天再次开单：返回原单，不新建
    resp = open_order(client, "A", day)
    assert "返回原单 #1" in resp.get_data(as_text=True)
    assert client.get("/supplement/2").status_code == 404


def test_open_order_rejects_unfinished_day(client):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    seed_hourly(client, "A", today, {"00": 1})
    assert "尚未结束" in open_order(client, "A", today).get_data(as_text=True)
    assert "尚未结束" in open_order(client, "A", tomorrow).get_data(as_text=True)


def test_supplement_upload_fills_missing_hours(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)
    r = usage(client, "A", day, day)
    assert day_kwh(r, day) == "0"            # 只有首条读数，尚无增量
    assert r["days"][0]["pending_hours"] == 23

    csv_text = (
        f"point,ts,value\nA,{day}T01:00:00Z,105\nA,{day}T02:00:00Z,107\n"
    )
    resp = upload_order(client, 1, csv_text)
    body = resp.get_data(as_text=True)
    assert "补抄单 #1 上传成功" in body and "新增读数 2 条" in body

    r = usage(client, "A", day, day)
    # 增量计入后次读数所属日：0 点→1 点 5，1 点→2 点 2
    assert day_kwh(r, day) == "7"
    assert r["days"][0]["pending_hours"] == 21
    body = order_page(client, 1)
    assert "常规到数" not in body            # 补抄读数不算“常规到数”
    assert body.count("现场已核") >= 2


def test_supplement_only_accepts_order_point_and_hours(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)

    # 别的计量点
    resp = upload_order(
        client, 1, f"point,ts,value\nB,{day}T01:00:00Z,50\n"
    )
    assert "只收计量点 A" in resp.get_data(as_text=True)
    # 别的日期
    other = closed_day(2)
    resp = upload_order(
        client, 1, f"point,ts,value\nA,{other}T05:00:00Z,50\n"
    )
    assert "只收" in resp.get_data(as_text=True) and "整批拒绝" in resp.get_data(as_text=True)
    # 非整点
    resp = upload_order(
        client, 1, f"point,ts,value\nA,{day}T05:30:00Z,50\n"
    )
    assert "不在本单上" in resp.get_data(as_text=True)
    # 单外整点（00 点开单时已有读数，不在单上）
    resp = upload_order(
        client, 1, f"point,ts,value\nA,{day}T00:00:00Z,100\n"
    )
    assert "不在本单上" in resp.get_data(as_text=True)

    r = usage(client, "A", day, day)
    assert day_kwh(r, day) == "0"            # 全部被拒，数据不变


def test_supplement_regular_upload_marks_regular(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)
    # 常规上传照常收数，落在单上整点
    upload(client, "readings", f"point,ts,value\nA,{day}T01:00:00Z,104\n")
    body = order_page(client, 1)
    assert "常规到数" in body
    r = usage(client, "A", day, day)
    assert day_kwh(r, day) == "4"
    assert r["days"][0]["pending_hours"] == 22


def test_supplement_same_value_registers_verification_without_double_count(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)
    upload(client, "readings", f"point,ts,value\nA,{day}T02:00:00Z,130\n")
    r = usage(client, "A", day, day)
    assert day_kwh(r, day) == "30"

    # 现场回报与已入库读数同值：登记核验，电量不重复计
    resp = upload_order(client, 1, f"point,ts,value\nA,{day}T02:00:00Z,130\n")
    body = resp.get_data(as_text=True)
    assert "登记核验 1 条" in body
    assert "现场已核" in order_page(client, 1)
    r = usage(client, "A", day, day)
    assert day_kwh(r, day) == "30"           # 只算一次
    assert r["days"][0]["pending_hours"] == 22

    # 再次原样回报：核验也幂等
    resp = upload_order(client, 1, f"point,ts,value\nA,{day}T02:00:00Z,130\n")
    assert "核验重复 1 条" in resp.get_data(as_text=True)
    assert day_kwh(usage(client, "A", day, day), day) == "30"


def test_supplement_conflict_and_failed_batch_keeps_everything(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)
    assert "新增读数 1 条" in upload_order(
        client, 1, f"point,ts,value\nA,{day}T01:00:00Z,110\n"
    ).get_data(as_text=True)
    before = usage(client, "A", day, day)
    assert day_kwh(before, day) == "10"

    # 同刻不同值 → 整批拒绝，原读数、核验、用量版本都不变
    resp = upload_order(
        client, 1,
        f"point,ts,value\nA,{day}T02:00:00Z,200\nA,{day}T01:00:00Z,111\n",
    )
    assert "已存在不同读数" in resp.get_data(as_text=True)
    after = usage(client, "A", day, day)
    assert after["total_kwh"] == before["total_kwh"]
    assert after["days"][0]["pending_hours"] == before["days"][0]["pending_hours"]
    assert after["version"] == before["version"]   # 失败不推进版本
    body = order_page(client, 1)
    assert "现场已核" in body                      # 先前核验保留
    assert body.count("待补") >= 1                 # 失败批次没有登记 02 点


def test_supplement_backwards_reading_rejected(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100, "02": 130})
    open_order(client, "A", day)
    resp = upload_order(client, 1, f"point,ts,value\nA,{day}T01:00:00Z,200\n")
    assert "读数倒退" in resp.get_data(as_text=True)
    assert day_kwh(usage(client, "A", day, day), day) == "30"


def test_usage_pages_and_api_show_pending_hours_for_opened_days(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)
    r = usage(client, "A", day, day)
    assert r["days"][0]["pending_hours"] == 23
    assert r["days"][0]["order_id"] == 1
    assert r["orders"] == [
        {"order_id": 1, "day": day, "total_hours": 23, "pending_hours": 23}
    ]
    body = client.get(
        f"/usage?point=A&start={day}&end={day}"
    ).get_data(as_text=True)
    assert "仍缺 23 个整点" in body

    # 未开单日期：pending_hours 为 null，不虚构数字
    r2 = usage(client, "A", closed_day(3), closed_day(3))
    assert r2["days"][0]["pending_hours"] is None
    assert r2["orders"] == []


def test_supplement_batches_accumulate_and_verification_beats_regular(client):
    day = closed_day()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)
    # 分批：先补 01，再补 02
    assert "新增读数 1 条" in upload_order(
        client, 1, f"point,ts,value\nA,{day}T01:00:00Z,110\n"
    ).get_data(as_text=True)
    assert "新增读数 1 条" in upload_order(
        client, 1, f"point,ts,value\nA,{day}T02:00:00Z,125\n"
    ).get_data(as_text=True)
    r = usage(client, "A", day, day)
    assert day_kwh(r, day) == "25"
    assert r["days"][0]["pending_hours"] == 21

    # 常规上传同一整点同值：读数不重复，单上仍为现场已核
    upload(client, "readings", f"point,ts,value\nA,{day}T01:00:00Z,110\n")
    assert day_kwh(usage(client, "A", day, day), day) == "25"
    resp = upload_order(client, 1, f"point,ts,value\nA,{day}T01:00:00Z,110\n")
    assert "核验重复 1 条" in resp.get_data(as_text=True)
    body = order_page(client, 1)
    assert "现场已核" in body
    assert day_kwh(usage(client, "A", day, day), day) == "25"


def test_concurrent_supplement_uploads_idempotent(app):
    """并发按单补抄同一批读数：不重复计数，也不产生冲突。"""
    day = closed_day()
    client = app.test_client()
    seed_hourly(client, "A", day, {"00": 100})
    open_order(client, "A", day)
    csv_text = f"point,ts,value\nA,{day}T01:00:00Z,110\nA,{day}T02:00:00Z,130\n"
    errors = []

    def do_upload():
        c = app.test_client()
        try:
            resp = upload_order(c, 1, csv_text)
            if resp.status_code != 200:
                errors.append(resp.status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    threads = [threading.Thread(target=do_upload) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    r = usage(app.test_client(), "A", day, day)
    assert day_kwh(r, day) == "30"
    assert r["days"][0]["pending_hours"] == 21
