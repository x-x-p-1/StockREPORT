# 财报因子数据库 — SQLite 建表
# 使用: python init_db.py
# 生成: F:\QUANT\数据\代码\财报爬虫\finance.db

import sqlite3
import os
from pathlib import Path

DB_DIR = Path(r'F:\QUANT\数据\代码\财报爬虫')
DB_PATH = DB_DIR / 'finance.db'

DB_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA = """
-- 每只股票每季度一条记录
CREATE TABLE IF NOT EXISTS quarterly (
    code        TEXT NOT NULL,          -- 6位代码, e.g. '688017'
    name        TEXT,                   -- 简称
    report_date TEXT NOT NULL,          -- 报告期, e.g. '2026-06-30'
    crawl_time  TEXT DEFAULT (datetime('now','localtime')),

    -- 核心财务 (mootdx finance 37字段精选)
    eps         REAL,                   -- 每股收益
    bvps        REAL,                   -- 每股净资产
    roe         REAL,                   -- 净资产收益率(%)
    profit      REAL,                   -- 净利润 (万元)
    income      REAL,                   -- 主营收入 (万元)
    gross_margin REAL,                  -- 毛利率(%) — 从收入/成本推算
    total_shares REAL,                  -- 总股本 (万股)
    float_shares REAL,                  -- 流通股本 (万股)
    net_assets  REAL,                   -- 每股净资产
    reserve     REAL,                   -- 每股公积金
    undistributed REAL,                 -- 每股未分配利润

    -- YTD 表现 (从日线算，写入时填入)
    ytd_return  REAL,                   -- 年初至今涨跌幅 (%)
    ytd_rank    INTEGER,                -- 排名 (1=涨幅最大)

    -- 唯一约束 (保证同代码同报告期不重复)
    UNIQUE(code, report_date)
);

CREATE INDEX IF NOT EXISTS idx_code ON quarterly(code);
CREATE INDEX IF NOT EXISTS idx_date ON quarterly(report_date);
CREATE INDEX IF NOT EXISTS idx_ytd  ON quarterly(ytd_rank);
"""

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f'✅ 数据库初始化完成: {DB_PATH}')

if __name__ == '__main__':
    init_db()
