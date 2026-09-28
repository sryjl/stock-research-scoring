# -*- coding: utf-8 -*-
"""一次性迁移：从 Dragon Score 旧库搬运「原始数据」到本项目的干净库。

只搬运可复用的原始表，丢弃旧模型的分数/排名/状态/回测结果。
源库只读（只 SELECT，不写）。目标库：data/dragon/dragon_v2.sqlite
"""
import os
import sqlite3
import time

SRC_MAIN = r"C:\Users\明静流\.codex\.chatgpt-projects\g-p-6aa97eb562848191ae89ab3fd1c2c3dc\data\dragon_score_v1_app\dragon_score_v1.sqlite"
SRC_OUTCOME = r"C:\Users\明静流\.codex\.chatgpt-projects\g-p-6aa97eb562848191ae89ab3fd1c2c3dc\data\dragon_score_v1_outcome\outcome-v1.sqlite"

BASE = os.path.dirname(os.path.abspath(__file__))
DEST_DIR = os.path.join(BASE, "data", "dragon")
DEST = os.path.join(DEST_DIR, "dragon_v2.sqlite")

# (源文件, 附加别名, 要搬运的表)
PLAN = [
    (SRC_MAIN, "main_src",
     ["stock_metadata", "recent_bar", "potential_bar",
      "dragon_score_snapshot", "potential_snapshot"]),
    (SRC_OUTCOME, "outcome_src", ["outcome_snapshot"]),
]


def main():
    os.makedirs(DEST_DIR, exist_ok=True)
    if os.path.exists(DEST):
        os.remove(DEST)
        print("已删除旧目标库，重新开始")

    dest = sqlite3.connect(DEST)
    dest.execute("PRAGMA journal_mode=OFF")
    dest.execute("PRAGMA synchronous=OFF")
    dest.execute("PRAGMA cache_size=-262144")  # 256MB 缓存
    dest.execute("PRAGMA temp_store=MEMORY")

    for src, alias, _ in PLAN:
        dest.execute(f'ATTACH DATABASE ? AS {alias}', (src,))

    t0 = time.time()
    for src, alias, tables in PLAN:
        for t in tables:
            row = dest.execute(
                f'SELECT sql FROM {alias}.sqlite_master WHERE type="table" AND name=?', (t,)
            ).fetchone()
            if row is None:
                print(f"[跳过] 源表不存在 {alias}.{t}")
                continue
            dest.execute(row[0])  # 按源库 DDL 建表（保留主键/约束）
            st = time.time()
            dest.execute(f'INSERT INTO "{t}" SELECT * FROM {alias}."{t}"')
            n = dest.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"[OK] {t}: {n} 行, 耗时 {time.time()-st:.1f}s")

    # 搬运索引
    for src, alias, tables in PLAN:
        for t in tables:
            for idx_name, idx_sql in dest.execute(
                f'SELECT name, sql FROM {alias}.sqlite_master '
                f'WHERE type="index" AND tbl_name=? AND sql IS NOT NULL', (t,)
            ):
                try:
                    dest.execute(idx_sql)
                except Exception as e:
                    print(f"[索引跳过] {idx_name}: {e}")

    dest.commit()
    dest.execute("PRAGMA journal_mode=DELETE")
    dest.close()

    size = os.path.getsize(DEST) / (1024 ** 3)
    print(f"\n完成，总耗时 {time.time()-t0:.1f}s，目标库 {size:.2f} GB")
    print(DEST)


if __name__ == "__main__":
    main()
