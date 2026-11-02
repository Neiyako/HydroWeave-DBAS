"""资源保护层。

明确边界（重要）：
- 这里的阈值是「尽量早停」的软保护，不是绝对安全保证。操作系统级的内存
  管理与磁盘 I/O 无法被本层完全控制；RSS 达标到停止接收之间仍有窗口期。
- 真正的风险控制依赖：低默认并发（单进程）、小批次、时间预算、
  任务级心跳与断点续跑，以及对危险解析的子进程隔离（后续阶段）。
- 所有判断都可测试：ResourceGuard.sample() 返回纯数据，decide() 是纯函数。
"""
from __future__ import annotations

from dataclasses import dataclass

import psutil

from dbas_core.logging import get_logger

log = get_logger("resource")


@dataclass
class ResourceSample:
    rss_mb: float
    available_mb: float | None   # 系统可用内存（部分容器环境拿不到）
    percent: float | None


@dataclass
class ResourceDecision:
    stop_new_tasks: bool
    reason: str


def sample(process: psutil.Process | None = None) -> ResourceSample:
    proc = process or psutil.Process()
    vm = psutil.virtual_memory()
    return ResourceSample(
        rss_mb=proc.memory_info().rss / 1024 / 1024,
        available_mb=vm.available / 1024 / 1024,
        percent=vm.percent,
    )


def decide(s: ResourceSample, *, max_rss_mb: float,
           min_available_mb: float) -> ResourceDecision:
    """根据采样判断是否应停止接收新任务（纯函数，便于测试）。"""
    if s.rss_mb >= max_rss_mb:
        return ResourceDecision(True, f"进程 RSS {s.rss_mb:.0f}MB ≥ 警戒线 {max_rss_mb:.0f}MB")
    if s.available_mb is not None and s.available_mb <= min_available_mb:
        return ResourceDecision(
            True, f"系统可用内存 {s.available_mb:.0f}MB ≤ 下限 {min_available_mb:.0f}MB")
    return ResourceDecision(False, "")


class ResourceGuard:
    """运行期资源监视：由 Runner 在每处理完一个文件后轮询一次。"""

    def __init__(self, max_rss_mb: float, min_available_mb: float):
        self.max_rss_mb = max_rss_mb
        self.min_available_mb = min_available_mb
        self.peak_rss_mb = 0.0
        self._proc = psutil.Process()
        self.stopped_reason: str | None = None

    def check(self) -> ResourceDecision:
        s = sample(self._proc)
        self.peak_rss_mb = max(self.peak_rss_mb, s.rss_mb)
        d = decide(s, max_rss_mb=self.max_rss_mb,
                   min_available_mb=self.min_available_mb)
        if d.stop_new_tasks:
            self.stopped_reason = d.reason
            log.warning("资源保护触发，停止接收新任务：%s", d.reason)
        return d
