"""数据库结构定义。

所有表只保存稳定引用、数量与摘要；身份信息以脱敏引用方式存放。
台账、库存移动与事件日志为只追加表，通过触发器禁止修改和删除。
"""

SCHEMA_VERSION = "2"

DDL = """
CREATE TABLE IF NOT EXISTS service_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 主体与组织 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS person (
    person_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN
                ('manager','inspector','reviewer','supervisor',
                 'technician','sales','finance')),
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS base (
    base_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    region      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS farmer (
    farmer_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    base_id         TEXT NOT NULL REFERENCES base(base_id),
    id_code_digest  TEXT,
    joined_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS water_body (
    water_body_id   TEXT PRIMARY KEY,
    base_id         TEXT NOT NULL REFERENCES base(base_id),
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pond (
    pond_id         TEXT PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    water_body_id   TEXT NOT NULL REFERENCES water_body(water_body_id),
    farmer_id       TEXT NOT NULL REFERENCES farmer(farmer_id),
    area_mu         REAL NOT NULL CHECK (area_mu > 0),
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','CLOSED')),
    created_at      TEXT NOT NULL
);

-- 苗种与放养 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS seed_lot (
    seed_lot_id     TEXT PRIMARY KEY,
    species         TEXT NOT NULL,
    supplier_ref    TEXT NOT NULL,
    qty_seed        REAL NOT NULL CHECK (qty_seed > 0),
    produced_at     TEXT NOT NULL,
    cert_ref        TEXT,
    payload_digest  TEXT
);

CREATE TABLE IF NOT EXISTS stocking (
    stocking_id     TEXT PRIMARY KEY,
    seed_lot_id     TEXT NOT NULL REFERENCES seed_lot(seed_lot_id),
    pond_id         TEXT NOT NULL REFERENCES pond(pond_id),
    qty             REAL NOT NULL CHECK (qty > 0),
    stocked_at      TEXT NOT NULL,
    recorder_id     TEXT REFERENCES person(person_id)
);

-- 投入品与技术指导 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS material (
    material_code   TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN
                    ('FEED','VET_DRUG','FERTILIZER','DISINFECTANT','OTHER')),
    organic_allowed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS input_application (
    application_id  TEXT PRIMARY KEY,
    pond_id         TEXT NOT NULL REFERENCES pond(pond_id),
    material_code   TEXT NOT NULL REFERENCES material(material_code),
    material_lot    TEXT,
    qty             REAL NOT NULL CHECK (qty > 0),
    unit            TEXT NOT NULL,
    applied_at      TEXT NOT NULL,
    recorder_id     TEXT REFERENCES person(person_id),
    evidence_digest TEXT
);

CREATE TABLE IF NOT EXISTS guidance (
    guidance_id     TEXT PRIMARY KEY,
    pond_id         TEXT NOT NULL REFERENCES pond(pond_id),
    technician_id   TEXT NOT NULL REFERENCES person(person_id),
    topic           TEXT NOT NULL,
    content_ref     TEXT,
    occurred_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certification (
    cert_id         TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL CHECK (subject_type IN
                    ('WATER_BODY','POND','FARMER','BASE')),
    subject_id      TEXT NOT NULL,
    cert_no         TEXT NOT NULL,
    issuer_ref      TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','SUSPENDED','EXPIRED','REVOKED')),
    document_digest TEXT
);

-- 五重检测：苗种 / 水体 / 投入品 / 成品 / 出口 ------------------------------
CREATE TABLE IF NOT EXISTS inspection (
    inspection_id   TEXT PRIMARY KEY,
    stage           TEXT NOT NULL
                    CHECK (stage IN ('SEED','WATER','INPUT','PRODUCT','EXPORT')),
    subject_type    TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    result          TEXT NOT NULL CHECK (result IN ('PASS','FAIL','PENDING')),
    inspector_id    TEXT NOT NULL REFERENCES person(person_id),
    sampled_at      TEXT NOT NULL,
    resulted_at     TEXT,
    items_json      TEXT NOT NULL DEFAULT '[]',
    report_digest   TEXT
);
CREATE INDEX IF NOT EXISTS idx_inspection_subject
    ON inspection(subject_type, subject_id);

-- 冻结单与签署 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS freeze_order (
    freeze_id       TEXT PRIMARY KEY,
    inspection_id   TEXT NOT NULL REFERENCES inspection(inspection_id),
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'FROZEN'
                    CHECK (status IN ('FROZEN','RELEASED','DESTROYED')),
    created_by      TEXT NOT NULL REFERENCES person(person_id),
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS freeze_target (
    freeze_id       TEXT NOT NULL REFERENCES freeze_order(freeze_id),
    target_type     TEXT NOT NULL CHECK (target_type IN ('WATER_BODY','BATCH')),
    target_id       TEXT NOT NULL,
    PRIMARY KEY (freeze_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_freeze_target ON freeze_target(target_type, target_id);

CREATE TABLE IF NOT EXISTS freeze_action (
    action_id           TEXT PRIMARY KEY,
    freeze_id           TEXT NOT NULL REFERENCES freeze_order(freeze_id),
    action              TEXT NOT NULL
                        CHECK (action IN ('FREEZE','RETEST','RELEASE','DESTROY')),
    signer_id           TEXT NOT NULL REFERENCES person(person_id),
    signer_role         TEXT NOT NULL,
    result              TEXT CHECK (result IN ('PASS','FAIL')),
    note                TEXT,
    signed_at           TEXT NOT NULL,
    retest_inspection_id TEXT REFERENCES inspection(inspection_id)
);

-- 起捕、分级 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS harvest_batch (
    harvest_id      TEXT PRIMARY KEY,
    pond_id         TEXT NOT NULL REFERENCES pond(pond_id),
    seed_lot_id     TEXT REFERENCES seed_lot(seed_lot_id),
    farmer_id       TEXT NOT NULL REFERENCES farmer(farmer_id),
    qty_kg          REAL NOT NULL CHECK (qty_kg > 0),
    caught_at       TEXT NOT NULL,
    method          TEXT,
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','DESTROYED'))
);

CREATE TABLE IF NOT EXISTS grade_record (
    grade_id        TEXT PRIMARY KEY,
    harvest_id      TEXT NOT NULL REFERENCES harvest_batch(harvest_id),
    grade           TEXT NOT NULL,
    qty_kg          REAL NOT NULL CHECK (qty_kg > 0),
    graded_at       TEXT NOT NULL,
    graded_by       TEXT REFERENCES person(person_id)
);

-- 批次树（起捕批 / 拆分子批 / 退货批），数量守恒 ----------------------------
CREATE TABLE IF NOT EXISTS split_group (
    split_group_id  TEXT PRIMARY KEY,
    parent_batch_id TEXT NOT NULL REFERENCES batch(batch_id),
    consumed_kg     REAL NOT NULL CHECK (consumed_kg > 0),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch (
    batch_id        TEXT PRIMARY KEY,
    parent_batch_id TEXT REFERENCES batch(batch_id),
    split_group_id  TEXT REFERENCES split_group(split_group_id),
    root_harvest_id TEXT REFERENCES harvest_batch(harvest_id),
    origin_batch_id TEXT REFERENCES batch(batch_id),
    kind            TEXT NOT NULL CHECK (kind IN ('HARVEST','SPLIT','RETURN')),
    grade           TEXT,
    qty_kg          REAL NOT NULL CHECK (qty_kg >= 0),
    allocated_kg    REAL NOT NULL DEFAULT 0,
    shipped_kg      REAL NOT NULL DEFAULT 0,
    destroyed_kg    REAL NOT NULL DEFAULT 0,
    split_out_kg    REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'AVAILABLE'
                    CHECK (status IN ('AVAILABLE','FROZEN','DESTROYED','EXHAUSTED')),
    created_at      TEXT NOT NULL,
    CHECK (qty_kg >= allocated_kg + shipped_kg + destroyed_kg + split_out_kg)
);
CREATE INDEX IF NOT EXISTS idx_batch_parent ON batch(parent_batch_id);
CREATE INDEX IF NOT EXISTS idx_batch_root ON batch(root_harvest_id);
CREATE INDEX IF NOT EXISTS idx_batch_origin ON batch(origin_batch_id);

-- 只追加库存移动
CREATE TABLE IF NOT EXISTS stock_movement (
    movement_id     TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES batch(batch_id),
    delta_kg        REAL NOT NULL,
    ref_type        TEXT NOT NULL CHECK (ref_type IN
                    ('PRODUCE','SPLIT_OUT','SPLIT_IN','ALLOCATE_HOLD',
                     'ALLOC_RELEASE','SHIP','DESTROY','RETURN_IN','FREEZE_HOLD')),
    ref_id          TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movement_batch ON stock_movement(batch_id);

CREATE TRIGGER IF NOT EXISTS trg_movement_no_update BEFORE UPDATE ON stock_movement
BEGIN SELECT RAISE(ABORT, '库存移动为只追加记录，禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_movement_no_delete BEFORE DELETE ON stock_movement
BEGIN SELECT RAISE(ABORT, '库存移动为只追加记录，禁止删除'); END;

-- 出口采购 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS export_order (
    order_id        TEXT PRIMARY KEY,
    customer_ref    TEXT NOT NULL,
    sales_id        TEXT NOT NULL REFERENCES person(person_id),
    grade_required  TEXT NOT NULL,
    conditions_json TEXT NOT NULL DEFAULT '{}',
    containers_count INTEGER NOT NULL CHECK (containers_count > 0),
    kg_per_container REAL NOT NULL CHECK (kg_per_container > 0),
    status          TEXT NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN','CONFIRMED','COMPLETED','CANCELLED')),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_container (
    container_id    TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES export_order(order_id),
    seq             INTEGER NOT NULL,
    container_no    TEXT,
    target_kg       REAL NOT NULL CHECK (target_kg > 0),
    allocated_kg    REAL NOT NULL DEFAULT 0,
    shipped_kg      REAL NOT NULL DEFAULT 0,
    returned_kg     REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'PLANNING'
                    CHECK (status IN ('PLANNING','ALLOCATED','SHIPPED',
                                      'RECEIVED','RETURNED')),
    UNIQUE (order_id, seq)
);

CREATE TABLE IF NOT EXISTS batch_allocation (
    allocation_id   TEXT PRIMARY KEY,
    container_id    TEXT NOT NULL REFERENCES order_container(container_id),
    batch_id        TEXT NOT NULL REFERENCES batch(batch_id),
    qty_kg          REAL NOT NULL CHECK (qty_kg > 0),
    shipped_kg      REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'HELD'
                    CHECK (status IN ('HELD','SHIPPED','RELEASED')),
    created_at      TEXT NOT NULL,
    UNIQUE (container_id, batch_id)
);

CREATE TABLE IF NOT EXISTS shipment (
    shipment_id     TEXT PRIMARY KEY,
    allocation_id   TEXT NOT NULL REFERENCES batch_allocation(allocation_id),
    qty_kg          REAL NOT NULL CHECK (qty_kg > 0),
    shipped_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_receipt (
    receipt_id      TEXT PRIMARY KEY,
    container_id    TEXT NOT NULL UNIQUE
                    REFERENCES order_container(container_id),
    delivered_kg    REAL NOT NULL CHECK (delivered_kg >= 0),
    received_by     TEXT NOT NULL,
    delivered_at    TEXT NOT NULL,
    evidence_digest TEXT
);

CREATE TABLE IF NOT EXISTS sales_return (
    return_id       TEXT PRIMARY KEY,
    receipt_id      TEXT NOT NULL REFERENCES delivery_receipt(receipt_id),
    batch_id        TEXT NOT NULL REFERENCES batch(batch_id),
    qty_kg          REAL NOT NULL CHECK (qty_kg > 0),
    reason          TEXT NOT NULL,
    created_by      TEXT REFERENCES person(person_id),
    created_at      TEXT NOT NULL
);

-- 合同与结算（台账不可改） --------------------------------------------------
CREATE TABLE IF NOT EXISTS contract (
    contract_id             TEXT PRIMARY KEY,
    farmer_id               TEXT NOT NULL REFERENCES farmer(farmer_id),
    version                 INTEGER NOT NULL,
    effective_from          TEXT NOT NULL,
    grade_prices_json       TEXT NOT NULL,
    support_premium_per_kg  REAL NOT NULL DEFAULT 0,
    is_current              INTEGER NOT NULL DEFAULT 1,
    terms_digest            TEXT,
    created_at              TEXT NOT NULL,
    UNIQUE (farmer_id, version)
);

CREATE TABLE IF NOT EXISTS settlement (
    settlement_id   TEXT PRIMARY KEY,
    farmer_id       TEXT NOT NULL REFERENCES farmer(farmer_id),
    contract_id     TEXT NOT NULL REFERENCES contract(contract_id),
    contract_version INTEGER NOT NULL,
    period          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','CONFIRMED','REVERSED')),
    generated_at    TEXT NOT NULL,
    confirmed_at    TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS settlement_line (
    line_id         TEXT PRIMARY KEY,
    settlement_id   TEXT NOT NULL REFERENCES settlement(settlement_id),
    harvest_id      TEXT NOT NULL REFERENCES harvest_batch(harvest_id),
    grade           TEXT NOT NULL,
    qty_kg          REAL NOT NULL CHECK (qty_kg > 0),
    unit_price      REAL NOT NULL,
    premium_per_kg  REAL NOT NULL,
    amount          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entry (
    entry_id        TEXT PRIMARY KEY,
    settlement_id   TEXT REFERENCES settlement(settlement_id),
    line_id         TEXT REFERENCES settlement_line(line_id),
    harvest_id      TEXT REFERENCES harvest_batch(harvest_id),
    farmer_id       TEXT NOT NULL REFERENCES farmer(farmer_id),
    direction       TEXT NOT NULL CHECK (direction IN ('NORMAL','REVERSAL')),
    amount          REAL NOT NULL,
    grade           TEXT,
    qty_kg          REAL,
    unit_price      REAL,
    premium_per_kg  REAL,
    reason          TEXT NOT NULL,
    ref_type        TEXT,
    ref_id          TEXT,
    reverses_entry_id TEXT REFERENCES ledger_entry(entry_id),
    created_by      TEXT REFERENCES person(person_id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_farmer ON ledger_entry(farmer_id);
CREATE INDEX IF NOT EXISTS idx_ledger_harvest ON ledger_entry(harvest_id, grade);

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update BEFORE UPDATE ON ledger_entry
BEGIN SELECT RAISE(ABORT, '结算台账只允许冲正，禁止修改原账'); END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete BEFORE DELETE ON ledger_entry
BEGIN SELECT RAISE(ABORT, '结算台账只允许冲正，禁止删除原账'); END;

-- 市场抽检样品 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_sample (
    sample_id       TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES batch(batch_id),
    market_ref      TEXT NOT NULL,
    sampled_at      TEXT NOT NULL,
    items_json      TEXT NOT NULL DEFAULT '[]',
    report_digest   TEXT
);

-- 交换事件（只追加，来源序号单调，保留原始发生时间） --------------------------
CREATE TABLE IF NOT EXISTS event_log (
    event_id        TEXT PRIMARY KEY,
    source_ref      TEXT NOT NULL,
    source_sequence INTEGER NOT NULL,
    subject_ref     TEXT,
    action          TEXT,
    occurred_at     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    payload_digest  TEXT NOT NULL,
    result_ref      TEXT,
    UNIQUE (source_ref, source_sequence)
);
CREATE TRIGGER IF NOT EXISTS trg_event_no_update BEFORE UPDATE ON event_log
BEGIN SELECT RAISE(ABORT, '事件日志为只追加记录'); END;
CREATE TRIGGER IF NOT EXISTS trg_event_no_delete BEFORE DELETE ON event_log
BEGIN SELECT RAISE(ABORT, '事件日志为只追加记录'); END;
"""
