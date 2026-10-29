# 运行手册与数据安全

## 首次使用安全检查清单

- [ ] 确认原始数据盘为只读习惯使用：DBAS 自身从不写原始卷，但不要把
      `data/` 目录建在原始数据盘上。
- [ ] `python -m dbas status` 确认指向预期项目根。
- [ ] 注册卷时用 `--id` 显式指定稳定 ID（跨重挂载不变）。
- [ ] 第一次扫描先 `--mode inventory`（只盘点，不解析），确认文件数符合预期。
- [ ] 用 `--budget 60 --batch-size 20` 小预算试跑元数据解析，观察输出。

## 硬盘离线怎么办

- 扫描时卷不可访问 → 报错退出（exit code 3），**不会**把文件标记为已删除。
- 文件只有在「卷在线 + 完整扫描成功」后仍未发现，才被标记
  `missing_pending`（待确认缺失），等你人工确认。
- 卷被显式标记 offline 后其文件显示 `offline` 状态；重新挂载后
  `volume register` 同一 ID 即恢复 online，文件状态在下次扫描自动恢复。

## 断点续跑与失败处理

- budget/batch/资源水位触发的中断都会打印恢复提示；**重跑同一命令即可**。
- 查看失败任务：`tasks list --status failed`，诊断信息在 `tasks show ID`。
- 修复问题后重跑：failed 任务不会被自动重试；如需重试，删除该任务
  （`sqlite3 data/catalog.db "DELETE FROM tasks WHERE task_id='...'"`）后
  重跑扫描命令即可重新入队（dedup_key 含 mtime，文件没变也会重新入队的是
  新任务；详见 tests/test_scanner.py::test_incremental_only_enqueues_changed）。
- 进程被强杀：`running` 任务在下次启动时自动回收为 `pending`。

## 资源保护的真实边界

- `resource_guard` 监控进程 RSS 与系统可用内存，达到警戒线后**不再认领新任务**，
  当前正在处理的文件会正常完成并提交。
- 这不是绝对安全保证：OS 的内存管理、swap、磁盘 I/O 调度不受本程序控制；
  RSS 检查间隔为「每文件一次」，极端巨大的单文件元数据仍可能瞬时超线。
- 因此真正的保护是组合拳：单进程默认并发 + 小批次 + 时间预算 + 每文件小事务
  提交 + 断点续跑。请勿把 budget 调到数小时不中断地跑大盘。
- BLAS/数值库线程通过环境变量限制为 1（`core/config.py` 顶层设置）。

## 数据库维护

- 备份：`python -m dbas db backup`（SQLite 在线备份 API，可在使用中执行）。
- 检查：`python -m dbas db check`（integrity_check + foreign_key_check）。
- 重建：`db backup` → 移走 `data/catalog.db` → 重新注册卷并扫描。
  原始数据不受影响；唯一损失是任务历史，文件索引由原始文件重建。

## 已声明的未实现项

QC、EDA、清洗、特征工程命令尚未提供（避免「看起来有」的假接口）；
CDS 服务器排队时间不可控，fetch 不设硬超时，中断后重跑即可。
