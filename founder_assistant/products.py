"""Product master: one product per real-world item, many aliases.

Matching is deterministic (exact accent-free key or registered alias) — no fuzzy guessing,
so 'thịt heo' / 'heo' / 'thịt lợn' resolve to one product while 'thịt bò' never
silently merges into it. Unknown names create a new product and raise an info alert.
"""
from __future__ import annotations

from .db import DB
from .textnorm import display_name, norm_key

# canonical name -> (category, default_unit, aliases)
# Aliases are TRUE SYNONYMS only (same item, same price basis). Different cuts, breeds or varieties
# (thịt heo nạc vs ba chỉ, gà ta vs gà công nghiệp, tôm sú vs tôm thẻ, hành lá vs hành tím) are NOT
# aliased: they become separate products, and the Founder merges them explicitly with "gộp a = b".
SEED_PRODUCTS: dict[str, tuple[str, str | None, list[str]]] = {
    "Thịt heo": ("thịt", "kg", ["heo", "thịt lợn", "lợn"]),
    "Thịt bò": ("thịt", "kg", ["bò"]),
    "Thịt gà": ("thịt", "kg", ["gà"]),
    "Tôm": ("hải sản", "kg", []),
    "Tôm sú": ("hải sản", "kg", []),
    "Tôm thẻ": ("hải sản", "kg", []),
    "Tôm càng": ("hải sản", "kg", ["tôm càng xanh"]),
    "Mực": ("hải sản", "kg", []),
    "Rau": ("rau củ", None, []),
    "Rau cải": ("rau củ", "kg", []),
    "Hủ tiếu": ("tinh bột", "kg", ["hủ tíu"]),
    "Trứng gà": ("trứng", None, []),
    "Nước mắm": ("gia vị", "chai", []),
    "Đường": ("gia vị", "kg", []),
    "Tỏi": ("rau củ", "kg", []),
    "Gas": ("chi phí", "bình", ["bình gas", "ga"]),
}


class ProductMaster:
    def __init__(self, db: DB):
        self.db = db

    def seed(self, now: str) -> None:
        for name, (cat, unit, aliases) in SEED_PRODUCTS.items():
            pid = self._ensure(name, cat, unit, now)
            for a in aliases:
                self.add_alias(a, pid)
        self.db.commit()

    def _ensure(self, name: str, category: str | None, unit: str | None, now: str) -> int:
        key = norm_key(name)
        row = self.db.one("SELECT id FROM products WHERE key = ?", (key,))
        if row:
            return int(row["id"])
        pid = self.db.insert(
            "products", {"name": name, "key": key, "category": category, "default_unit": unit, "created_at": now}
        )
        self.add_alias(name, pid)
        return pid

    def add_alias(self, alias: str, product_id: int) -> None:
        self.db.execute(
            "INSERT INTO product_aliases(alias_key, alias, product_id) VALUES (?, ?, ?) "
            "ON CONFLICT(alias_key) DO UPDATE SET product_id = excluded.product_id",
            (norm_key(alias), alias, product_id),
        )

    def match(self, raw_name: str) -> int | None:
        key = norm_key(raw_name)
        if not key:
            return None
        row = self.db.one("SELECT product_id FROM product_aliases WHERE alias_key = ?", (key,))
        if row:
            return int(row["product_id"])
        row = self.db.one("SELECT id FROM products WHERE key = ?", (key,))
        return int(row["id"]) if row else None

    def resolve(self, raw_name: str, default_unit: str | None, now: str) -> tuple[int, bool]:
        """Return (product_id, created_new)."""
        pid = self.match(raw_name)
        if pid is not None:
            return pid, False
        pid = self._ensure(display_name(raw_name), None, default_unit, now)
        return pid, True

    def merge_alias(self, alias: str, target_name: str, now: str) -> tuple[int, int | None]:
        """Founder command 'gộp <alias> = <target>'. Moves history of the alias product onto target.

        Returns (target_id, merged_product_id or None).
        """
        target_id = self.match(target_name)
        if target_id is None:
            target_id = self._ensure(display_name(target_name), None, None, now)
        old_id = self.match(alias)
        self.add_alias(alias, target_id)
        merged = None
        if old_id is not None and old_id != target_id:
            for table in ("purchase_items", "price_history"):
                self.db.execute(f"UPDATE {table} SET product_id = ? WHERE product_id = ?", (target_id, old_id))
            self.db.execute("UPDATE product_aliases SET product_id = ? WHERE product_id = ?", (target_id, old_id))
            merged = old_id
        self.db.commit()
        return target_id, merged

    def name(self, product_id: int) -> str:
        row = self.db.one("SELECT name FROM products WHERE id = ?", (product_id,))
        return row["name"] if row else f"#{product_id}"
