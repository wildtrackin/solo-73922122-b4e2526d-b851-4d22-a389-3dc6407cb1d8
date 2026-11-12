# 固件安装 API（Firmware Installation API）

面向设备发布工程师的固件推送服务：通过 **FastAPI + SQLite + cryptography** 实现带
**Ed25519 清单签名校验、防降级、单任务互斥、崩溃可恢复** 的安装工作流，并附带
设备模拟器与本地签名样例，可直接用 Docker Compose 启动并在 `/docs` 演示。

---

## 1. 它保证了什么

| 需求 | 实现方式 |
| --- | --- |
| 上传 ≤ 8 MiB 固件 + JSON 清单 + 覆盖清单原始字节的 Ed25519 签名 | `POST /installations`（multipart），超限返回 `413` |
| 可信公钥 / 设备地址 / 型号固定配置 | `app/config.py`，仅由环境变量/配置注入，绝不接受请求方传入 |
| 清单含型号、正整数版本、固件 SHA-256 | 严格校验类型（拒绝布尔版本、非十六进制摘要、非正整数版本） |
| 完整验证后才下发 | 签名→JSON→字段→型号→固件摘要全部通过后才写库并下发 |
| 设备只接受更高版本 | 设备模拟器在版本比较处强制；API 下发前再做一次防降级/冲突预检 |
| 同版同摘要返回原结果 | 查询设备确认 `(version, sha256)` 已安装 → 直接 SUCCESS（幂等，不重发字节） |
| 同版异摘要拒绝 | API 预检与设备侧均拒绝，任务记 `REJECTED` |
| 一次只执行一笔安装 | PENDING/DISPATCHED/VERIFYING 任一开放任务占用唯一槽位，新上传返回 `409` |
| 结果未核实前拒绝新安装，但状态可查 | 槽位锁定期间 `GET` 状态查询与 `/health` 始终可用 |
| 回执丢失/进程退出后保留原任务 | 固件与清单原始字节、签名整包持久化于 SQLite |
| 重启先查设备：已安装补记成功，未安装重发原包 | 启动 `recover_on_startup` + 后台 `reconcile` 循环 + 手动 `POST /reconcile` |
| 超时标为待核实，不能报失败或放行下一笔 | 设备不可达 → `VERIFYING`，槽位保持锁定，绝不置为失败 |

---

## 2. 任务状态机

```
                 (校验全部通过, 写入 SQLite)
   上传 ───────────────────────────────► PENDING
                                           │ 下发
                                           ▼
                                        DISPATCHED
                          ┌────────────────┴──────────────────┐
                   收到成功回执 installed / already_installed  │ 无回执/超时/5xx
                          ▼                                    ▼
                       SUCCESS ◄──────── 核实/重启恢复 ───── VERIFYING（待核实，槽位锁定）
                          ▲                                    │
                          │  设备拒绝（降级/同版异摘要）        │ 设备恢复后重发原包
                          └──────── REJECTED ◄─────────────────┘
```

- **PENDING / DISPATCHED / VERIFYING** 为“开放状态”，共同占用唯一安装槽位。
- **SUCCESS / REJECTED** 为终态，释放槽位。
- 任何“不知道设备到底装没装”的情况一律落到 `VERIFYING`，而不是失败。

---

## 3. 目录结构

```
.
├── app/                    # 安装 API
│   ├── config.py           # 固定配置（公钥路径、设备地址、型号、8MiB 上限）
│   ├── crypto_utils.py     # Ed25519 验签/签名
│   ├── db.py               # SQLite 持久层（含固件 BLOB、开放任务查询）
│   ├── device_client.py    # 设备 HTTP 客户端（超时即“不可达”）
│   ├── service.py          # 校验、下发、防降级、恢复/核实编排
│   ├── models.py           # Pydantic 响应模型
│   └── main.py             # FastAPI 应用工厂与路由
├── device_sim/             # 网关设备模拟器（独立服务）
│   └── app.py              # 版本/摘要原子切换、幂等、冲突、丢回执注入
├── keygen/generate_keys.py # 生成固定 Ed25519 信任锚（演示密钥对）
├── signing/                # 本地签名样例
│   ├── sample_sign.py      # 构造固件+清单+签名
│   └── verify_sample.py    # 仅用公钥离线验签
├── demo/
│   ├── run_demo.py         # 容器内一键端到端演示客户端
│   └── demo.sh             # 面向宿主机 curl 的演示脚本
├── tests/                  # 20 个测试（校验/状态机/恢复/设备契约）
├── Dockerfile.api
├── Dockerfile.device
└── docker-compose.yml
```

---

## 4. 快速开始（Docker Compose）

```bash
# 构建并启动：keygen（一次性生成密钥）→ device → api
docker compose up --build

# 另开一个终端，运行端到端演示（含丢回执、409、恢复）
docker compose run --rm demo
```

服务地址：

- 安装 API 与交互文档：<http://localhost:8000/docs>
- 设备模拟器状态：<http://localhost:8001/status>

数据均落在命名卷（`api-data`、`device-data`、`keys`），**重启/重建容器后任务与设备状态保留**，
可直接观察崩溃恢复。

### 中断恢复手工演示

```bash
# 1) 让设备“激活后丢回执”
curl -X POST http://localhost:8001/admin/lose-next-receipt \
  -H 'Content-Type: application/json' -d '{"enabled": true}'

# 2) 上传一笔安装（用 signing 工具先签名，见下节），任务会进入 VERIFYING
# 3) 此时重启 API 进程
docker compose restart api
# 4) 查看日志：启动即查询设备，已安装则补记 SUCCESS；未安装则重发原包
docker compose logs api
```

---

## 5. 本地签名与上传

### 5.1 生成密钥（Compose 已自动执行；本地手动方式）

```bash
KEYS_DIR=./keys python -m keygen.generate_keys
```

### 5.2 构造并签名一个固件包

```bash
python -m signing.sample_sign --keys-dir keys --out build/v2 \
    --model gateway-gx100 --version 2 --size 4096
# 产物：build/v2/{firmware.bin, manifest.json, signature.b64}
```

签名覆盖的是清单的**原始字节**（规范的紧凑 JSON），验证时按同样的字节做 Ed25519 验签。

### 5.3 上传

```bash
curl -X POST http://localhost:8000/installations \
  -F firmware=@build/v2/firmware.bin \
  -F manifest=@build/v2/manifest.json;type=application/json \
  -F signature="$(cat build/v2/signature.b64)"
# 202 -> {"task_id":..., "state":"PENDING", ...}

# 查询状态（槽位锁定期间也可用）
curl http://localhost:8000/installations/<task_id>

# 立即触发一次核实/重发
curl -X POST http://localhost:8000/installations/reconcile
```

### 5.4 离线验签（不经过 API）

```bash
python -m signing.verify_sample --keys-dir keys \
  --manifest build/v2/manifest.json --signature build/v2/signature.b64
```

---

## 6. HTTP 接口摘要

| 方法 | 路径 | 说明 | 关键状态码 |
| --- | --- | --- | --- |
| POST | `/installations` | 上传固件+清单+签名 | `202` 受理 / `400` 校验失败 / `409` 槽位忙 / `413` 超 8MiB |
| GET | `/installations/{id}` | 查询单笔任务（始终可用） | `200` / `404` |
| GET | `/installations` | 最近任务列表 | `200` |
| POST | `/installations/reconcile` | 立即核实开放任务：已安装补记成功，否则重发原包 | `200` |
| GET | `/health` | 存活与当前槽位占用者 | `200` |

设备模拟器管理接口（仅演示/测试用）：

- `POST /admin/lose-next-receipt`：下一笔真实激活后丢弃回执（模拟回执丢失）。
- `POST /admin/reset`：恢复出厂版本与摘要。
- `GET /status`：当前型号、版本、摘要、安装时间。

---

## 7. 安全与正确性要点

1. **信任锚固定**：公钥路径、设备基址、目标型号均为服务端固定配置，请求无法覆盖。
2. **先验签后解析业务字段**：签名校验在 JSON 解析之前完成，杜绝篡改清单。
3. **摘要自证**：固件 SHA-256 由服务端重新计算并与清单比对，不信任客户端声明。
4. **严格类型**：版本必须是正整数（`bool` 因是 `int` 子类被显式拒绝）。
5. **双重防降级**：API 下发前用设备当前状态预检，设备侧再强制版本规则。
6. **未知即待核实**：任何拿不到可信回执的情况（超时、连接失败、5xx、回执无法解析）
   都进入 `VERIFYING`，保持槽位锁定，绝不谎报失败、绝不放行下一笔。
7. **整包持久化**：固件 BLOB 与清单/签名原样入库，重发不依赖客户端再次上传，
   保证“重发的是原包”。
8. **单进程串行化 + SQLite 事务**：进程内用异步锁保证槽位检查与建任务原子；
   WAL 模式保证进程退出后状态可被新进程一致读取。

---

## 8. 运行测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

覆盖内容（20 项）：

- 坏签名、篡改清单、错误型号、摘要不符、非正整数/布尔版本、非法 JSON、超 8MiB；
- 同版同摘要幂等、同版异摘要拒绝、降级拒绝；
- 丢回执 → VERIFYING → 核实补记 SUCCESS；未安装 → 重发原包；
- 设备持续不可达时始终 VERIFYING 且槽位不释放，恢复后完成原任务；
- 模拟进程退出后新进程打开同一数据库的启动恢复（两条分支）；
- 设备模拟器自身的幂等/冲突/降级/丢回执/摘要不符契约。

---

## 9. 配置项（环境变量，前缀 `FW_`）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `FW_DATABASE_PATH` | `/data/installer.db` | SQLite 数据库路径 |
| `FW_TRUSTED_PUBLIC_KEY_PATH` | `/keys/public.pem` | 固定 Ed25519 公钥 |
| `FW_DEVICE_BASE_URL` | `http://device:8001` | 固定目标设备地址 |
| `FW_DEVICE_MODEL` | `gateway-gx100` | 固定目标型号 |
| `FW_MAX_FIRMWARE_BYTES` | `8388608` | 固件硬上限 8 MiB |
| `FW_DEVICE_TIMEOUT_SECONDS` | `5.0` | 设备请求超时（超时即待核实） |
| `FW_RECONCILE_INTERVAL_SECONDS` | `3.0` | 后台核实循环间隔 |

> 演示密钥对仅用于本地/Compose。生产环境应将 Ed25519 私钥保存在离线签名系统，
> 服务端只保留公钥。
