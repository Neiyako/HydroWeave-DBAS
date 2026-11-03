"""任务执行器（Runner）。

- 从任务队列原子认领任务，逐个执行，受三类边界约束：
  1) 时间预算 budget_s（每个文件处理完才检查，中断点永远是安全边界）；
  2) 批次上限 batch_limit；
  3) 资源警戒线（ResourceGuard，每文件后轮询）。
- 单个任务失败不影响整批；重试到 max_attempts 后置 failed。
- 进程被杀：下次启动 reclaim_stale_tasks 回收 running 任务，无状态残留。
- budget 耗尽安全退出，并打印恢复命令。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from dbas_core.catalog.repository import Catalog, VolumeOffline
from dbas_core.logging import get_logger
from dbas.resource_guard.guard import ResourceGuard

log = get_logger("runner")

# handler(task_params, catalog, ctx) -> result dict
Handler = Callable[[dict, Catalog, "RunContext"], dict]


@dataclass
class RunContext:
    """传给 handler 的运行上下文：handler 应在每处理完一个文件后调用 heartbeat()。"""
    catalog: Catalog
    worker: str

    def heartbeat(self) -> None:
        if self._task_id:
            self.catalog.heartbeat_task(self._task_id)

    _task_id: str | None = None


@dataclass
class RunSummary:
    run_id: str
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_retry: int = 0
    stop_reason: str = ""          # budget/batch/resource/queue_empty
    elapsed_s: float = 0.0
    peak_rss_mb: float = 0.0
    errors: list[str] = field(default_factory=list)

    def resume_hint(self) -> str | None:
        if self.stop_reason in ("budget", "batch", "resource"):
            return ("本次未处理完，直接重跑同一命令即可从断点继续"
                    "（pending/retry_wait 任务会自动认领）。")
        return None


class Runner:
    def __init__(self, catalog: Catalog, handlers: dict[str, Handler],
                 *, budget_s: float, batch_limit: int,
                 guard: ResourceGuard | None = None,
                 retry_delay_s: float = 5.0,
                 stale_running_s: float = 600.0,
                 resume: bool = True):
        self.catalog = catalog
        self.handlers = handlers
        self.budget_s = budget_s
        self.batch_limit = batch_limit
        self.guard = guard
        self.retry_delay_s = retry_delay_s
        self.stale_running_s = stale_running_s
        self.resume = resume

    def run(self, types: list[str] | None = None) -> RunSummary:
        summary = RunSummary(run_id=uuid.uuid4().hex[:12])
        start = time.time()
        if self.resume:
            stale = self.catalog.reclaim_stale_tasks(stale_s=self.stale_running_s)
            if stale:
                log.warning("回收 %d 个中断遗留的 running 任务: %s",
                            len(stale), ",".join(stale))
        ctx = RunContext(catalog=self.catalog, worker=summary.run_id)
        while True:
            task = self.catalog.claim_task(ctx.worker, types=types)
            if task is None:
                summary.stop_reason = summary.stop_reason or "queue_empty"
                break
            summary.claimed += 1
            attempt_id = self.catalog.start_attempt(task["task_id"])
            ctx._task_id = task["task_id"]
            handler = self.handlers.get(task["type"])
            if handler is None:
                self.catalog.finish_task(
                    task["task_id"], "blocked",
                    error=f"未知任务类型: {task['type']}", attempt_id=attempt_id)
                summary.failed += 1
                summary.errors.append(f"{task['task_id']}: 未知任务类型 {task['type']}")
                continue
            try:
                result = handler(json.loads(task["params_json"]),
                                 self.catalog, ctx)
                self.catalog.finish_task(
                    task["task_id"], "succeeded", result=result,
                    attempt_id=attempt_id)
                summary.succeeded += 1
            except VolumeOffline as e:
                # 卷离线：本任务记失败，终止整批避免持续报错
                self.catalog.finish_task(
                    task["task_id"], "failed", error=str(e),
                    attempt_id=attempt_id)
                summary.failed += 1
                summary.stop_reason = "volume_offline"
                summary.errors.append(f"{task['task_id']}: {e}")
                break
            except Exception as e:  # noqa: BLE001 单任务失败隔离
                msg = f"{type(e).__name__}: {e}"
                summary.errors.append(f"{task['task_id']}: {msg}")
                if getattr(e, "permanent", False):
                    # 永久失败：重试必然以同样方式失败，直接落 failed 让人看到真因。
                    # 用属性而非 import 判断，避免 scheduler 反向依赖 fetch 层。
                    self.catalog.finish_task(
                        task["task_id"], "failed", error=msg,
                        attempt_id=attempt_id)
                    summary.failed += 1
                    log.error("任务 %s 永久失败，不重试: %s", task["task_id"], msg)
                elif task["attempts"] >= task["max_attempts"]:
                    self.catalog.finish_task(
                        task["task_id"], "failed", error=msg,
                        attempt_id=attempt_id)
                    summary.failed += 1
                    log.error("任务 %s 重试耗尽，标记 failed: %s",
                              task["task_id"], msg)
                else:
                    self._retry_wait(task, attempt_id, msg)
                    summary.skipped_retry += 1
            # ---- 边界检查（每任务后） ----
            summary.elapsed_s = time.time() - start
            if summary.stop_reason:
                pass
            elif summary.elapsed_s >= self.budget_s:
                summary.stop_reason = "budget"
            elif summary.claimed >= self.batch_limit:
                summary.stop_reason = "batch"
            elif self.guard is not None and self.guard.check().stop_new_tasks:
                summary.stop_reason = "resource"
            summary.peak_rss_mb = max(summary.peak_rss_mb,
                                      self.guard.peak_rss_mb if self.guard else 0.0)
            if summary.stop_reason and summary.stop_reason != "queue_empty":
                break
        summary.elapsed_s = time.time() - start
        return summary

    def _retry_wait(self, task: dict, attempt_id: int, msg: str) -> None:
        delay = self.retry_delay_s
        with_retry_at = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + delay))
        self.catalog.finish_task(
            task["task_id"], "retry_wait", error=f"{msg}（将于 {with_retry_at} 后可重试）",
            attempt_id=attempt_id)
        # 通过把 updated_at 推迟到重试时刻实现延迟（claim 会检查 updated_at <= now）
        self.catalog.conn.execute(
            "UPDATE tasks SET updated_at=? WHERE task_id=?",
            (with_retry_at, task["task_id"]))
        self.catalog.conn.commit()
