# 输变电装备制造交付平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入；并为输变电装备基地提供订单承诺、部件批次、产线能力、检验关口、运输窗口与工程变更的统一制造交付编排。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/manufacturing_delivery/`：变压器与成套开关设备的订单承诺、部件批次占用、替代部件双轨核验、产线排程、检验关口、运输窗口、工程变更换型与承诺回退；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m manufacturing_delivery.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析和芯片准入流程，不访问外部网络。

制造交付验收覆盖“衡阳基地同时交付变压器与成套开关设备”场景：关键铁芯批次被多个合同重复承诺时，协调员能看到部件、质量规则、客户约束、产线工时和运输窗口的冲突来源；替代铁芯在客户书面放行且满足质量等级规则后才进入候选；订单确认在单个 IMMEDIATE 事务内整体占用部件、工时与窗口，失败不留半成品；工程变更生效后已开工设备锁定原设计、未开工设备重排，并可回退比对承诺前后差异。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m manufacturing_delivery.api --database manufacturing.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON，写接口通过 `X-Actor-Id` 头识别操作人。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

### 制造交付编排接口摘要

- `POST /models`、`POST /capacity`、`POST /shipping-windows`：型号 BOM/工艺路线/检验关口、产线日工时、运输窗口；
- `POST /components/lots`、`.../release|reject`、`POST /substitute-rules`：部件批次质量准入与替代部件规则；
- `POST /orders`、`GET /orders/{id}/evaluation`、`POST /orders/{id}/confirm`、`GET /orders/{id}/plan`：订单提交、冲突预检、整体确认与承诺快照；
- `POST /units/{id}/start|complete`、`POST /units/{id}/inspections`、`POST /shipments`：开工、总装完成、顺序检验关口与窗口内发运；
- `POST /changes`、`POST /changes/{id}/apply|rollback`、`GET /changes/{id}/evaluation`：工程变更、未开工重排、已开工锁定、回退与承诺差异；
- `GET /reports/conflicts`、`GET /audit/chain`：冲突来源报告与哈希链式审计。
