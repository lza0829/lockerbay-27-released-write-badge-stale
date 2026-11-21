# 格湾（LockerBay）

**骨架：** 入柜/取件事件装载 → 格口占用物化 → 超时回收 → 占用账对账

- 领域：数据工程（去皮=事件装载 + 占用状态物化 + 超时闸/回收 + 占用核证）
- 端口：前端 **6100**，接口 **11100**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/lockerbay-NN-*`
- 非仓库库存 CRUD、非运单轨迹轴、非营业日封账、非会话窗切分

本地冒烟：

```powershell
cd claude-workspace/lockerbay/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11100

cd ../frontend
npm install
npm run dev
```

## 已释放占用拒收追加

`backend/app/modules/released_occ_append_reject`：

- **定死形态**——搁置落库用 `quarantine` 表，`reason='append_rejected'`；
  `member_json` 保留原始事件与载荷，并冗余 `event_id` / `occupancy_id`。
  被拒事件不写入 `parcel_events` 主通道。
- **失败闭**——入口识别分流：装载在物化前调用 `classify()`，事件显式指向
  （或明确落回）已 `released` / `closed` 占用时整条进搁置，不进物化；
  另有 SQLite 触发器 `trg_occupancies_released_immutable` 兜底，
  released 行的任何 UPDATE 直接 ABORT，整趟装载回滚。禁止把 released 改回 occupied。
- **只读核证**：`GET /api/released-rejects?occupancy_id=…`、
  `GET /api/occupancies/{id}/reject-proof`。
- **夹具**：`fixtures/released_append_reject.json`
  （`POST /api/ingest/run {"fixture":"released_append_reject.json"}`），
  验证指向已释放占用的事件进搁置、历史行不动。

## 写闸卸标（拒收即卸可写角标）

`backend/app/modules/occupancy_write_gate`：

- **定死形态**——独立 `occupancy_write_gate`（`writable` / `gate_rev` /
  `released_at` / `released_by_event_id` / `release_reason`）与
  `occupancy_write_gate_audit` 审计表。不往 `occupancies` 行加列，已释放
  占用行沿用不可变触发器。
- **提交形态**——拒收 + 卸写闸 + 写审计同一事务、缺一回滚
  （`record_reject` → `write_gate.release`，均为 SAVEPOINT）。
  回执 `rejected` 时该占用必为 `writable=false`，且必有一条
  `action='reject_release'` 审计（`occupancy_id` / `event_id` / `created_at`）。
- **误卸防护**——`release()` 只接受确实 `released` / `closed` 的占用；
  对 open 占用或不存在占用抛 `GateViolation`，其它 open 占用写闸不受影响。
- **只读核证接口**：`GET /api/write-gate/{occupancy_id}`（角标）、
  `GET /api/write-gates?writable=`（清单）、
  `GET /api/occupancies/{id}/gate-audits`（卸标审计）、
  `GET /api/occupancies/{id}/write-gate-proof`（角标 + 审计 + 拒收一致性）。
- **协同**——开账建闸（`ensure_gate`）、取件释放卸闸（`pickup_release`）、
  拒收卸闸（`reject_release`）；格阵 `GET /api/bay` 的 `cell.writable`
  与角标同源，已释放格不再亮可写点。旧库由 `seed.backfill_write_gates`
  补齐闸行与历史拒收审计。

测试：`cd backend && python -m pytest tests -q`

