# 安全策略 · Security Policy

## 支持范围

| 版本 | 是否接受安全更新 |
|---|---|
| 1.x | ✅ |
| < 1.0 | ❌ |

## 报告漏洞

**请不要通过公开 Issue 报告安全问题。**

请用 GitHub 的私密报告通道：
<https://github.com/Neiyako/HydroWeave-DBAS/security/advisories/new>

报告请包含：

- 受影响版本（`pyproject.toml` 中的 `version`）与操作系统
- 复现步骤或最小复现脚本
- 影响评估（是否可导致数据损坏、越权访问、任意代码执行）
- 如有修复建议，一并说明

## 响应时间

本项目由个人维护，**尽力在 7 天内**给出首次回复。严重问题（数据损坏、
凭证泄露）会优先处理。

## 安全设计说明

DBAS 处理的是**本地数据**，不提供网络服务。以下是几个与安全相关的设计选择：

- **原始卷只读**：DBAS 从不写入原始数据卷，所有产物写入项目侧 `data/`。
- **不落敏感凭证**：`~/.cdsapirc`（CDS 凭据）由用户自行管理，仓库的
  `.gitignore` 已排除 `.env`、`.cdsapirc`、`configs/local*.yaml`。
  DBAS 自身不保存、不打印 API Key。
- **抓包脱敏**：`fetch capture` 的抓包诊断工具内置敏感字段过滤
  （`authorization`、`cookie`、`x-api-key`、`token`、`secret`、`password` 等），
  只保留 allowlist 中的响应头。
- **不执行外部代码**：`zarr`/`netCDF` 解析只读取数据，不使用 `pickle`
  等可执行序列化格式。
- **SQL 参数化**：catalog 的所有查询使用参数绑定，不拼接字符串。

## 已知边界

- DBAS **不校验数据来源真实性**。索引到目录里的文件来自哪里，由使用者负责。
- `checksum` 用于**检测**损坏，不是防篡改签名：能读文件的人同样能改它。
- 数组层（Zarr）是派生物，可被外部修改；用 `dbas zarr verify` 检测改动，
  但该检测依赖登记时的指纹，指纹本身没有加密签名。
