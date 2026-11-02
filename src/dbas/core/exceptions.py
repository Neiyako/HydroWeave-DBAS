"""DBAS 异常类型体系。

所有自定义异常继承 DbasError，CLI 层统一捕获后给出可操作的提示。
"""
from __future__ import annotations


class DbasError(Exception):
    """DBAS 基础异常。"""


class ConfigError(DbasError):
    """配置缺失或非法。"""


class VolumeError(DbasError):
    """数据卷注册/解析失败。"""


class VolumeOfflineError(VolumeError):
    """数据卷未挂载或不可访问。"""


class CatalogError(DbasError):
    """目录数据库读写失败。"""


class ScanError(DbasError):
    """扫描过程错误。"""


class TaskError(DbasError):
    """任务队列错误。"""


class FetchError(DbasError):
    """ERA5 下载错误。"""


class AnalysisError(DbasError):
    """分析配置或区域定义错误。"""


class SubsetError(AnalysisError):
    """区域裁切执行错误。"""
