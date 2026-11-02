"""scheduler 测试：预算、重试、断点续跑、陈旧任务回收、资源判断。"""
from __future__ import annotations

import pytest

from dbas.resource_guard.guard import ResourceGuard, ResourceSample, decide
from dbas.scheduler.runner import Runner, RunContext


class FlakyHandler:
    """前 N 次调用失败，之后成功。"""

    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, params, catalog, ctx):
        self.calls += 1
        ctx.heartbeat()
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        return {"ok": True}


def _mk_cat(tmp_path):
    from dbas.catalog.repository import Catalog
    return Catalog(tmp_path / "t.db")


def test_retry_then_succeed(tmp_path):
    cat = _mk_cat(tmp_path)
    cat.enqueue_task("t", {"x": 1}, dedup_key="d1", max_attempts=3)
    h = FlakyHandler(fail_times=1)
    runner = Runner(cat, {"t": h}, budget_s=60, batch_limit=10, retry_delay_s=5)
    s1 = runner.run(types=["t"])
    assert s1.skipped_retry == 1 and s1.failed == 0
    # 模拟重试延迟到期（updated_at 拨回过去）
    cat.conn.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00'")
    cat.conn.commit()
    s2 = Runner(cat, {"t": h}, budget_s=60, batch_limit=10, retry_delay_s=5)\
        .run(types=["t"])
    assert s2.succeeded == 1
    task = cat.list_tasks()[0]
    assert task["status"] == "succeeded"


def test_retry_exhausted_marks_failed(tmp_path):
    cat = _mk_cat(tmp_path)
    cat.enqueue_task("t", {}, dedup_key="d", max_attempts=2)
    h = FlakyHandler(fail_times=99)
    runner = Runner(cat, {"t": h}, budget_s=60, batch_limit=10, retry_delay_s=5)
    s1 = runner.run(types=["t"])
    assert s1.skipped_retry == 1 and s1.failed == 0   # 第一次: retry_wait
    # 把 updated_at 拨回过去模拟延迟到期，重跑
    cat.conn.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00'")
    cat.conn.commit()
    s2 = Runner(cat, {"t": h}, budget_s=60, batch_limit=10, retry_delay_s=5)\
        .run(types=["t"])
    assert s2.failed == 1
    task = cat.list_tasks()[0]
    assert task["status"] == "failed"
    assert task["attempts"] == 2
    assert "boom" in task["last_error"]


def test_budget_exhaustion_and_resume(tmp_path):
    """预算耗尽安全退出 + 下次运行从断点继续。"""
    cat = _mk_cat(tmp_path)
    for i in range(5):
        cat.enqueue_task("t", {"i": i}, dedup_key=f"k{i}")
    h = FlakyHandler(fail_times=0)
    runner = Runner(cat, {"t": h}, budget_s=-1, batch_limit=3,
                    retry_delay_s=0)   # budget=-1 → 处理 1 个就超预算
    s1 = runner.run(types=["t"])
    assert s1.stop_reason == "budget"
    assert s1.succeeded == 1
    assert s1.resume_hint() is not None
    s2 = Runner(cat, {"t": h}, budget_s=60, batch_limit=100,
                retry_delay_s=0).run(types=["t"])
    assert s2.succeeded == 4
    assert s2.stop_reason == "queue_empty"


def test_batch_limit(tmp_path):
    cat = _mk_cat(tmp_path)
    for i in range(5):
        cat.enqueue_task("t", {}, dedup_key=f"k{i}")
    s = Runner(cat, {"t": FlakyHandler(0)}, budget_s=60, batch_limit=2,
               retry_delay_s=0).run(types=["t"])
    assert s.succeeded == 2 and s.stop_reason == "batch"


def test_single_failure_does_not_block_batch(tmp_path):
    """故障测试：一个永久失败任务不阻塞其余任务。"""
    cat = _mk_cat(tmp_path)
    for i in range(3):
        cat.enqueue_task("t", {"i": i}, dedup_key=f"k{i}", max_attempts=1)
    calls = {"n": 0}

    def handler(params, catalog, ctx):
        calls["n"] += 1
        if params["i"] == 1:
            raise RuntimeError("bad file")
        return {}

    s = Runner(cat, {"t": handler}, budget_s=60, batch_limit=10,
               retry_delay_s=0).run(types=["t"])
    assert s.succeeded == 2 and s.failed == 1


def test_stale_running_task_reclaimed(tmp_path):
    """进程被杀场景：running 任务被回收重新调度，状态一致。"""
    cat = _mk_cat(tmp_path)
    tid, _ = cat.enqueue_task("t", {}, dedup_key="k")
    assert cat.claim_task("worker-a")["task_id"] == tid
    # 模拟 worker 死亡（心跳停在远古）
    cat.conn.execute(
        "UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00' WHERE task_id=?", (tid,))
    cat.conn.commit()
    reclaimed = cat.reclaim_stale_tasks(stale_s=0)
    assert reclaimed == [tid]
    task = cat.get_task(tid)
    assert task["status"] == "pending"
    s = Runner(cat, {"t": FlakyHandler(0)}, budget_s=60, batch_limit=10,
               retry_delay_s=0).run(types=["t"])
    assert s.succeeded == 1


def test_cancel_task_removes_from_schedule(tmp_path):
    """取消的任务不再被认领（用于废弃参数写错的历史任务）。"""
    cat = _mk_cat(tmp_path)
    tid, _ = cat.enqueue_task("t", {}, dedup_key="k")
    assert cat.cancel_task(tid) is True
    assert cat.claim_task("w1") is None
    assert cat.get_task(tid)["status"] == "cancelled"
    # 已成功的不允许取消（保留结果审计）
    tid2, _ = cat.enqueue_task("t", {}, dedup_key="k2")
    cat.finish_task(tid2, "succeeded")
    assert cat.cancel_task(tid2) is False


def test_cancel_task_frees_dedup_key_for_reenqueue(tmp_path):
    """取消后必须能重新入队：否则修正参数后会被 UNIQUE 约束永久挡住。"""
    cat = _mk_cat(tmp_path)
    tid, created = cat.enqueue_task("download", {"area": [30, 110, 40, 120]}, dedup_key="d1")
    assert created is True
    assert cat.cancel_task(tid) is True
    tid2, created2 = cat.enqueue_task("download", {"area": [40, 110, 30, 120]}, dedup_key="d1")
    assert created2 is True
    assert tid2 != tid
    assert cat.get_task(tid2)["status"] == "pending"


def test_claim_is_atomic_no_double_execution(tmp_path):
    cat = _mk_cat(tmp_path)
    cat.enqueue_task("t", {}, dedup_key="k")
    t1 = cat.claim_task("w1")
    t2 = cat.claim_task("w2")
    assert t1 is not None and t2 is None      # 第二个 worker 抢不到


def test_enqueue_dedup(tmp_path):
    cat = _mk_cat(tmp_path)
    _, c1 = cat.enqueue_task("t", {}, dedup_key="same")
    _, c2 = cat.enqueue_task("t", {}, dedup_key="same")
    assert c1 and not c2
    assert len(cat.list_tasks()) == 1


def test_tasks_prune_keeps_queue_bounded(tmp_path):
    cat = _mk_cat(tmp_path)
    for i in range(5):
        cat.enqueue_task("t", {}, dedup_key=f"k{i}")
    Runner(cat, {"t": FlakyHandler(0)}, budget_s=60, batch_limit=10,
           retry_delay_s=0).run(types=["t"])
    cur = cat.conn.execute("DELETE FROM tasks WHERE status='succeeded'")
    cat.conn.commit()
    assert cur.rowcount == 5
    assert cat.count_tasks_by_status() == {}


# ---- 资源判断（纯函数） ------------------------------------------------------

def test_resource_decide_triggers_on_rss():
    d = decide(ResourceSample(rss_mb=4000, available_mb=8000, percent=30),
               max_rss_mb=3072, min_available_mb=1024)
    assert d.stop_new_tasks and "RSS" in d.reason


def test_resource_decide_triggers_on_low_available():
    d = decide(ResourceSample(rss_mb=100, available_mb=512, percent=95),
               max_rss_mb=3072, min_available_mb=1024)
    assert d.stop_new_tasks and "可用内存" in d.reason


def test_resource_decide_allows_normal():
    d = decide(ResourceSample(rss_mb=800, available_mb=4000, percent=50),
               max_rss_mb=3072, min_available_mb=1024)
    assert not d.stop_new_tasks


def test_resource_guard_records_peak(tmp_path):
    g = ResourceGuard(max_rss_mb=100000, min_available_mb=1)
    d = g.check()
    assert not d.stop_new_tasks
    assert g.peak_rss_mb > 0
