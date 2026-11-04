# CDS 队列阻塞证据（2026-11-04）

目的：说明本次三端 e2e 为什么走 RDH「重放通道」而不是新的 CDS submit。

| 时间 (UTC) | 事件 | 证据 |
|---|---|---|
| 12:31 | RDH `plan create`（31 天×00Z×clwc/ciwc×3 层）提交 CDS job `41f418b4…` | 该 job 一直 `accepted`，`updated` 停在创建时间；13:xx 查询返回 404（被服务端清除） |
| 12:48 | 重新提交 job `7916e552…` | 至 14:16 仍 `accepted`（>85 分钟未 `running`），随后主动 DELETE → `dismissed` |
| 13:16–13:37 | 多次重试 `plan create` | 全部 `rejected`（"Maximum number queued requests"，队列上限） |
| 14:37–14:38 | 最小请求（1 天×1 时次×3 层，~45 KB）直接提交 | 14:38 起 `accepted`，至 15:00 仍未启动 |
| 15:00 | 改用重放通道完成 e2e | 见 `e2e.md`：RDH 传输/校验/交付真实执行，数据为 2026-11-03 CDS 真实产物 |

结论：阻塞点在 ECMWF CDS 服务端队列（保护性限流），不是本机脚本或凭证错误；
`rdh plan create` 的提交/轮询行为、RDH 传输与完整性校验链路均已在本机验证。

恢复做法（CDS 队列正常后）：
```bash
cd ~/Desktop/era5数据dbas
./bin/dbas fetch rdh --config configs/fetch/rdh_sample.yaml      # 新提交 + CDS 结果 URL 下载
./bin/dbas scan --volume downloads --mode metadata
.venv/bin/python scripts/e2e/three_end_e2e.py --run-id <新的 run-id>
```
