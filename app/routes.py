import sqlite3
from datetime import timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import logic
from .db import get_db

bp = Blueprint("main", __name__)

KIND_NAMES = {"readings": "读数", "swaps": "换表", "supplement": "补抄"}


def data_version(conn):
    """数据版本号 = 最近一个生效批次 id。同一事务内读取，保证版本与内容一致。"""
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS v FROM uploads WHERE status = 'applied'"
    ).fetchone()["v"]


def list_points(conn):
    return [
        r["point"]
        for r in conn.execute(
            "SELECT point FROM ("
            "  SELECT point FROM readings UNION SELECT point FROM swaps"
            "  UNION SELECT point FROM supplement_orders"
            ") ORDER BY point"
        )
    ]


def record_rejected(conn, kind, filename, message, rows_received, order_id=None):
    """被拒批次单独落一条审计记录（不影响既有数据）；审计失败不掩盖原始错误。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, order_id, created_at) VALUES (?,?,?,?,?,0,?,?)",
            (kind, filename, "rejected", message, rows_received, order_id,
             logic.now_ts()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")


@bp.route("/")
def index():
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：页面看到的是同一版快照
    try:
        points = list_points(conn)
        uploads = conn.execute(
            "SELECT * FROM uploads ORDER BY id DESC LIMIT 50"
        ).fetchall()
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "index.html",
        points=points,
        uploads=uploads,
        version=version,
        kind_names=KIND_NAMES,
    )


@bp.route("/upload/<kind>", methods=["POST"])
def upload(kind):
    if kind not in KIND_NAMES:
        abort(404)
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.index"))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("上传被拒绝：文件编码须为 UTF-8", "error")
        return redirect(url_for("main.index"))

    conn = get_db()
    # 1) 解析与格式校验（不进事务）
    if kind == "supplement":
        return _upload_supplement(conn, f.filename, text)
    try:
        if kind == "readings":
            parsed, raw_count = logic.parse_readings_csv(text)
        else:
            parsed, raw_count = logic.parse_swaps_csv(text)
    except logic.CsvError as e:
        record_rejected(conn, kind, f.filename, str(e), 0)
        flash(f"上传被拒绝：{e}", "error")
        return redirect(url_for("main.index"))

    # 2) 单个写事务：原始记录与受影响结果一起生效；失败整体回滚，保持旧数据
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, f.filename, "applied", "", raw_count, logic.now_ts()),
        )
        upload_id = cur.lastrowid
        if kind == "readings":
            applied, skipped = logic.apply_readings(conn, upload_id, parsed)
        else:
            applied, skipped = logic.apply_swaps(conn, upload_id, parsed)
        msg = f"接收 {raw_count} 行，新增 {applied} 条，重复跳过 {skipped} 条"
        conn.execute(
            "UPDATE uploads SET rows_applied = ?, message = ? WHERE id = ?",
            (applied, msg, upload_id),
        )
        conn.execute("COMMIT")
        flash(f"{KIND_NAMES[kind]}上传成功：{msg}", "success")
    except logic.BatchRejected as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, kind, f.filename, str(e), raw_count)
        flash(f"上传被拒绝：{e}", "error")
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
    return redirect(url_for("main.index"))


def _upload_supplement(conn, filename, text):
    """按补抄单分批上传原格式读数 CSV。

    只收单上的计量点与整点；核验与新读数在同一事务一起生效，
    任何失败整体回滚，旧核验/读数/用量版本保持不变。
    """
    order_id = request.form.get("order_id", type=int)
    redirect_to = url_for("main.supplement_order", order_id=order_id) \
        if order_id else url_for("main.index")
    if not order_id:
        flash("请从补抄单页面选择要补抄的单", "error")
        return redirect(url_for("main.index"))
    order = conn.execute(
        "SELECT * FROM supplement_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        flash("补抄单不存在", "error")
        return redirect(url_for("main.index"))

    try:
        parsed, raw_count = logic.parse_order_csv(text)
    except logic.CsvError as e:
        record_rejected(conn, "supplement", filename, str(e), 0, order_id)
        flash(f"上传被拒绝：{e}", "error")
        return redirect(redirect_to)

    conn.execute("BEGIN IMMEDIATE")
    try:
        order = conn.execute(
            "SELECT * FROM supplement_orders WHERE id = ?", (order_id,)
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, order_id, created_at) VALUES (?,?,?,?,?,0,?,?)",
            ("supplement", filename, "applied", "", raw_count, order_id,
             logic.now_ts()),
        )
        upload_id = cur.lastrowid
        r_applied, r_skipped, v_applied, v_skipped = logic.apply_supplement(
            conn, upload_id, order, parsed
        )
        msg = (
            f"接收 {raw_count} 行，新增读数 {r_applied} 条，"
            f"已到数跳过 {r_skipped} 条；登记核验 {v_applied} 条，"
            f"核验重复 {v_skipped} 条"
        )
        conn.execute(
            "UPDATE uploads SET rows_applied = ?, message = ? WHERE id = ?",
            (r_applied, msg, upload_id),
        )
        conn.execute("COMMIT")
        flash(f"补抄单 #{order_id} 上传成功：{msg}", "success")
    except logic.BatchRejected as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, "supplement", filename, str(e), raw_count, order_id)
        flash(f"上传被拒绝：{e}", "error")
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
    return redirect(redirect_to)


@bp.route("/supplement")
def supplement():
    conn = get_db()
    conn.execute("BEGIN")
    try:
        points = list_points(conn)
        orders = []
        for o in logic.list_orders(conn):
            states = logic.slot_states(conn, o)
            orders.append(
                {
                    "order": o,
                    "total": len(states),
                    "pending": sum(1 for s in states if s["status"] == logic.SLOT_PENDING),
                    "regular": sum(1 for s in states if s["status"] == logic.SLOT_REGULAR),
                    "verified": sum(1 for s in states if s["status"] == logic.SLOT_VERIFIED),
                }
            )
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "supplement.html",
        points=points,
        orders=orders,
        last_closed_day=logic.last_closed_day().strftime("%Y-%m-%d"),
        version=version,
    )


@bp.route("/supplement/open", methods=["POST"])
def supplement_open():
    point = request.form.get("point", "").strip()
    day = request.form.get("day", "").strip()
    if not point or not day:
        flash("请选择计量点和已结束的 UTC 日", "error")
        return redirect(url_for("main.supplement"))
    try:
        day = logic.parse_day(day).strftime("%Y-%m-%d")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("main.supplement"))

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        order, created = logic.open_order(conn, point, day)
        conn.execute("COMMIT")
    except logic.OrderError as e:
        conn.execute("ROLLBACK")
        flash(f"无法开单：{e}", "error")
        return redirect(url_for("main.supplement"))
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
        return redirect(url_for("main.supplement"))
    if created:
        flash(
            f"已开补抄单 #{order['id']}：{point} / {day}（UTC）。"
            "请记下开单时缺读数的整点。",
            "success",
        )
    else:
        flash(f"{point} / {day} 已开过补抄单，返回原单 #{order['id']}。", "success")
    return redirect(url_for("main.supplement_order", order_id=order["id"]))


@bp.route("/supplement/<int:order_id>")
def supplement_order(order_id):
    conn = get_db()
    conn.execute("BEGIN")
    try:
        order = conn.execute(
            "SELECT * FROM supplement_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            abort(404)
        states = logic.slot_states(conn, order)
        _, _, total = logic.usage_range(conn, order["point"], order["day"], order["day"])
        uploads = conn.execute(
            "SELECT * FROM uploads WHERE order_id = ? ORDER BY id DESC", (order_id,)
        ).fetchall()
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "supplement_order.html",
        order=order,
        slots=states,
        total=total,
        uploads=uploads,
        version=version,
        pending_count=sum(1 for s in states if s["status"] == logic.SLOT_PENDING),
    )


def _resolve_range(conn, point, start, end):
    """校验/补全日期范围，返回 (start_day, end_day)。缺省时取该点数据最近 31 天。"""
    if start and end:
        d0, d1 = logic.parse_day(start), logic.parse_day(end)
    else:
        row = conn.execute(
            "SELECT MIN(substr(ts,1,10)) AS lo, MAX(substr(ts,1,10)) AS hi"
            " FROM readings WHERE point = ?",
            (point,),
        ).fetchone()
        if row is None or row["lo"] is None:
            raise ValueError("该计量点暂无读数，请指定日期范围")
        d1 = logic.parse_day(end) if end else logic.parse_day(row["hi"])
        d0 = logic.parse_day(start) if start else logic.parse_day(row["lo"])
        if (d1 - d0).days > 30:
            d0 = d1 - timedelta(days=30)
    if d1 < d0:
        raise ValueError("结束日期早于开始日期")
    if (d1 - d0).days > logic.MAX_RANGE_DAYS:
        raise ValueError(f"日期范围过大（最多 {logic.MAX_RANGE_DAYS} 天）")
    return d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")


@bp.route("/usage")
def usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：同一版完整结果
    try:
        points = list_points(conn)
        version = data_version(conn)
        days = gaps = total = orders = None
        error = None
        if point:
            try:
                start, end = _resolve_range(conn, point, start, end)
                days, gaps, total, orders = logic.usage_with_orders(
                    conn, point, start, end
                )
            except ValueError as e:
                error = str(e)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "usage.html",
        points=points,
        point=point,
        start=start,
        end=end,
        days=days,
        gaps=gaps,
        total=total,
        orders=orders,
        error=error,
        version=version,
    )


@bp.route("/api/usage")
def api_usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if not (point and start and end):
        return jsonify(error="需要 point、start、end 参数（日期为 YYYY-MM-DD）"), 400
    conn = get_db()
    conn.execute("BEGIN")
    try:
        try:
            start, end = _resolve_range(conn, point, start, end)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        days, gaps, total, orders = logic.usage_with_orders(
            conn, point, start, end
        )
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return jsonify(
        point=point,
        start=start,
        end=end,
        version=version,
        total_kwh=logic.fmt_num(total),
        days=[
            {
                "day": d["day"],
                "kwh": logic.fmt_num(d["kwh"]),
                "gap": d["gap"],
                "order_id": d["order_id"],
                "pending_hours": d["pending_hours"],
            }
            for d in days
        ],
        orders=[
            {
                "order_id": o["order_id"],
                "day": o["day"],
                "total_hours": o["total"],
                "pending_hours": o["pending"],
            }
            for o in orders
        ],
        gaps=[{"start": g["start"], "end": g["end"]} for g in gaps],
    )
