"""SQLite 存储层：连接、建表与迁移。

约定：
- 重量一律使用整数克（*_g），金额一律使用整数分（*_cents），避免浮点误差。
- 业务时间使用外部提供的带偏移量 ISO 8601 字符串，系统时间仅用于 created_at。
- 外部主体使用不含真实身份信息的稳定引用编号（*_ref），由调用方提供并保证唯一。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "2"

DDL = """
CREATE TABLE IF NOT EXISTS service_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 养殖户（帮扶档位影响结算加价，具体数值落在合同版本价格上）
CREATE TABLE IF NOT EXISTS farmer (
    farmer_ref   TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    base_region  TEXT NOT NULL,
    assist_tier  TEXT NOT NULL DEFAULT 'standard',
    created_at   TEXT NOT NULL
);

-- 养殖塘口，status 为派生状态缓存，权威依据是 freeze 表中的活动冻结
CREATE TABLE IF NOT EXISTS pond (
    pond_ref       TEXT PRIMARY KEY,
    farmer_ref     TEXT NOT NULL REFERENCES farmer(farmer_ref),
    base_region    TEXT NOT NULL,
    area_mu        REAL NOT NULL CHECK (area_mu > 0),
    water_body_ref TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'frozen')),
    created_at     TEXT NOT NULL
);

-- 收购合同按版本保存，结算按交付时间匹配生效版本，历史版本永不改写
CREATE TABLE IF NOT EXISTS contract (
    contract_ref   TEXT NOT NULL,
    version        INTEGER NOT NULL,
    farmer_ref     TEXT NOT NULL REFERENCES farmer(farmer_ref),
    effective_from TEXT NOT NULL,
    effective_to   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (contract_ref, version)
);

-- 每个合同版本下各等级的单价与帮扶加价（分/公斤）
CREATE TABLE IF NOT EXISTS contract_price (
    contract_ref     TEXT NOT NULL,
    version          INTEGER NOT NULL,
    grade            TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    assist_cents     INTEGER NOT NULL DEFAULT 0 CHECK (assist_cents >= 0),
    PRIMARY KEY (contract_ref, version, grade),
    FOREIGN KEY (contract_ref, version) REFERENCES contract(contract_ref, version)
);

-- 龙头企业统一供应的苗种批次，remaining 保证投放守恒
CREATE TABLE IF NOT EXISTS seedling_batch (
    seed_batch_ref TEXT PRIMARY KEY,
    supplied_by    TEXT NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    remaining      INTEGER NOT NULL CHECK (remaining >= 0),
    supplied_at    TEXT NOT NULL,
    digest         TEXT
);

-- 投苗记录：苗种批次 → 塘口
CREATE TABLE IF NOT EXISTS stocking (
    stock_ref      TEXT PRIMARY KEY,
    seed_batch_ref TEXT NOT NULL REFERENCES seedling_batch(seed_batch_ref),
    pond_ref       TEXT NOT NULL REFERENCES pond(pond_ref),
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    occurred_at    TEXT NOT NULL
);

-- 投入品记录（饲料、动保等），材料只保存受控引用与 sha256 摘要
CREATE TABLE IF NOT EXISTS input_record (
    input_ref       TEXT PRIMARY KEY,
    pond_ref        TEXT NOT NULL REFERENCES pond(pond_ref),
    kind            TEXT NOT NULL,
    material_ref    TEXT NOT NULL,
    material_digest TEXT NOT NULL,
    quantity        REAL NOT NULL CHECK (quantity > 0),
    unit            TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    recorded_by     TEXT NOT NULL
);

-- 技术指导记录
CREATE TABLE IF NOT EXISTS guidance (
    guidance_ref TEXT PRIMARY KEY,
    pond_ref     TEXT NOT NULL REFERENCES pond(pond_ref),
    advisor_ref  TEXT NOT NULL,
    summary      TEXT NOT NULL,
    occurred_at  TEXT NOT NULL
);

-- 捕捞批次（起捕），一次起捕对应一个批次
CREATE TABLE IF NOT EXISTS harvest_batch (
    batch_ref       TEXT PRIMARY KEY,
    pond_ref        TEXT NOT NULL REFERENCES pond(pond_ref),
    seed_batch_ref  TEXT REFERENCES seedling_batch(seed_batch_ref),
    gross_weight_g  INTEGER NOT NULL CHECK (gross_weight_g > 0),
    graded_weight_g INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'graded', 'frozen', 'destroyed')),
    occurred_at     TEXT NOT NULL
);

-- 分级批次：捕捞批次拆分而来，重量必须守恒；available_g 支撑并发分配
CREATE TABLE IF NOT EXISTS grade_lot (
    lot_ref      TEXT PRIMARY KEY,
    batch_ref    TEXT NOT NULL REFERENCES harvest_batch(batch_ref),
    grade        TEXT NOT NULL,
    weight_g     INTEGER NOT NULL CHECK (weight_g > 0),
    available_g  INTEGER NOT NULL CHECK (available_g >= 0),
    status       TEXT NOT NULL DEFAULT 'available'
                 CHECK (status IN ('available', 'frozen', 'destroyed'))
);
CREATE INDEX IF NOT EXISTS idx_grade_lot_batch ON grade_lot(batch_ref);

-- 抽检记录（五重检测 round 取 1..5），可作用于塘口、捕捞批次或分级批次
CREATE TABLE IF NOT EXISTS inspection (
    inspection_ref TEXT PRIMARY KEY,
    scope_type     TEXT NOT NULL CHECK (scope_type IN ('pond', 'harvest_batch', 'grade_lot')),
    scope_ref      TEXT NOT NULL,
    round          INTEGER NOT NULL CHECK (round BETWEEN 1 AND 5),
    result         TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    method_ref     TEXT,
    inspector_ref  TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    digest         TEXT
);
CREATE INDEX IF NOT EXISTS idx_inspection_scope ON inspection(scope_type, scope_ref);

-- 冻结记录：检测不合格只冻结相关水体与关联批次，同一对象同时只允许一条活动冻结
CREATE TABLE IF NOT EXISTS freeze (
    freeze_ref            TEXT PRIMARY KEY,
    scope_type            TEXT NOT NULL CHECK (scope_type IN ('pond', 'batch')),
    scope_ref             TEXT NOT NULL,
    reason_inspection_ref TEXT NOT NULL REFERENCES inspection(inspection_ref),
    frozen_by             TEXT NOT NULL,
    frozen_at             TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'released', 'destroyed'))
);
CREATE INDEX IF NOT EXISTS idx_freeze_scope ON freeze(scope_type, scope_ref, status);

-- 冻结处置签署：复检、解封、销毁分别由不同角色签署，全部留痕
CREATE TABLE IF NOT EXISTS freeze_action (
    action_ref  TEXT PRIMARY KEY,
    freeze_ref  TEXT NOT NULL REFERENCES freeze(freeze_ref),
    action      TEXT NOT NULL CHECK (action IN ('retest', 'release', 'destroy')),
    result      TEXT CHECK (result IN ('pass', 'fail')),
    actor_ref   TEXT NOT NULL,
    role        TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_freeze_action ON freeze_action(freeze_ref);

-- 外商采购订单：柜量折算为需求总量 total_quantity_g，质量条件为可接受等级集合
CREATE TABLE IF NOT EXISTS export_order (
    order_ref        TEXT PRIMARY KEY,
    buyer_ref        TEXT NOT NULL,
    containers       INTEGER NOT NULL CHECK (containers > 0),
    total_quantity_g INTEGER NOT NULL CHECK (total_quantity_g > 0),
    allocated_g      INTEGER NOT NULL DEFAULT 0 CHECK (allocated_g >= 0),
    quality_grades   TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'partial', 'fulfilled', 'closed')),
    created_at       TEXT NOT NULL
);

-- 订单分配：从分级批次扣减可用量，条件更新保证并发不超卖
CREATE TABLE IF NOT EXISTS export_allocation (
    alloc_ref    TEXT PRIMARY KEY,
    order_ref    TEXT NOT NULL REFERENCES export_order(order_ref),
    lot_ref      TEXT NOT NULL REFERENCES grade_lot(lot_ref),
    quantity_g   INTEGER NOT NULL CHECK (quantity_g > 0),
    status       TEXT NOT NULL DEFAULT 'allocated'
                 CHECK (status IN ('allocated', 'delivered', 'cancelled')),
    allocated_at TEXT NOT NULL,
    settled_ref  TEXT
);
CREATE INDEX IF NOT EXISTS idx_allocation_lot ON export_allocation(lot_ref);
CREATE INDEX IF NOT EXISTS idx_allocation_order ON export_allocation(order_ref);

-- 交付回执：实际交付数量是结算依据
CREATE TABLE IF NOT EXISTS delivery_receipt (
    receipt_ref    TEXT PRIMARY KEY,
    alloc_ref      TEXT NOT NULL REFERENCES export_allocation(alloc_ref),
    received_qty_g INTEGER NOT NULL CHECK (received_qty_g >= 0),
    received_at    TEXT NOT NULL,
    receiver_ref   TEXT NOT NULL,
    note           TEXT
);

-- 结算单：生成后不改写，退货以冲正行追加
CREATE TABLE IF NOT EXISTS settlement (
    settlement_ref  TEXT PRIMARY KEY,
    farmer_ref      TEXT NOT NULL REFERENCES farmer(farmer_ref),
    contract_ref    TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    period          TEXT NOT NULL,
    total_cents     INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

-- 结算行：earning 为应收，reversal 为退货冲正（金额为负，指向原行）
CREATE TABLE IF NOT EXISTS settlement_line (
    line_ref          TEXT PRIMARY KEY,
    settlement_ref    TEXT NOT NULL REFERENCES settlement(settlement_ref),
    kind              TEXT NOT NULL CHECK (kind IN ('earning', 'reversal')),
    alloc_ref         TEXT REFERENCES export_allocation(alloc_ref),
    grade             TEXT,
    weight_g          INTEGER NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    assist_cents      INTEGER NOT NULL,
    amount_cents      INTEGER NOT NULL,
    reverses_line_ref TEXT REFERENCES settlement_line(line_ref),
    reason            TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlement_line ON settlement_line(settlement_ref);

-- 领域事件：每次变更按 contracts/ 约定的信封留痕，来源序号在本服务内递增
CREATE TABLE IF NOT EXISTS domain_event (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    subject_ref     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    source_sequence INTEGER NOT NULL,
    payload_digest  TEXT NOT NULL
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开一个连接。使用自动提交模式，事务由服务层显式 BEGIN IMMEDIATE 控制。"""
    database_path = path or os.environ.get("DATABASE_PATH", "data/app.sqlite3")
    connection = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def migrate(path: str | None = None) -> str:
    """初始化或升级数据库，返回数据库文件路径。可重复执行。"""
    database_path = Path(path or os.environ.get("DATABASE_PATH", "data/app.sqlite3"))
    if str(database_path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(str(database_path))
    try:
        connection.executescript(DDL)
        connection.execute(
            "INSERT INTO service_meta(key, value) VALUES('schema_version', ?)"

            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
    finally:
        connection.close()
    return str(database_path)
