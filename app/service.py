"""业务逻辑：草稿修订（乐观锁）与发布（一致视图内递归汇总）。

约定：
- 所有写操作在 BEGIN IMMEDIATE 事务内完成，失败即回滚，不留半成品。
- 发布时递归解析配方依赖，克数用 Fraction 精确折算，最后才转浮点输出。
"""
import json
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_num(fr: Fraction):
    """Fraction -> int/float，保留 6 位小数。"""
    if fr.denominator == 1:
        return fr.numerator
    return round(float(fr), 6)


# ---------------------------------------------------------------- 入参校验

def _parse_name(payload: dict) -> str:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "BAD_NAME", "name 必须是非空字符串")
    return name.strip()


def _parse_allergens(payload: dict) -> list:
    allergens = payload.get("allergens", [])
    if not isinstance(allergens, list):
        raise ApiError(400, "BAD_ALLERGENS", "allergens 必须是字符串数组")
    result = []
    for a in allergens:
        if not isinstance(a, str) or not a.strip():
            raise ApiError(400, "BAD_ALLERGENS", "allergens 元素必须是非空字符串")
        a = a.strip().lower()
        if a not in result:
            result.append(a)
    return result


def _parse_expected_version(payload: dict, required: bool):
    v = payload.get("expected_version")
    if v is None:
        if required:
            raise ApiError(400, "VERSION_REQUIRED", "修订必须携带所见版本 expected_version")
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ApiError(400, "BAD_VERSION", "expected_version 必须是正整数")
    return v


def _parse_grams(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(422, "NON_POSITIVE_AMOUNT", f"{where}: grams 必须是数字")
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        raise ApiError(422, "NON_POSITIVE_AMOUNT", f"{where}: grams 必须为正数，收到 {value!r}")
    return float(value)


def _parse_components(conn, payload: dict) -> list:
    """校验组分列表；任何一项非法都拒绝且不落库。"""
    comps = payload.get("components")
    if not isinstance(comps, list):
        raise ApiError(400, "BAD_COMPONENTS", "components 必须是数组")
    parsed = []
    for i, c in enumerate(comps):
        where = f"components[{i}]"
        if not isinstance(c, dict):
            raise ApiError(400, "BAD_COMPONENTS", f"{where} 必须是对象")
        ref_type = c.get("type")
        if ref_type not in ("ingredient", "recipe"):
            raise ApiError(400, "BAD_COMPONENTS", f"{where}.type 必须是 ingredient 或 recipe")
        ref_id = c.get("id")
        if isinstance(ref_id, bool) or not isinstance(ref_id, int) or ref_id < 1:
            raise ApiError(400, "BAD_COMPONENTS", f"{where}.id 必须是正整数")
        grams = _parse_grams(c.get("grams"), where)
        table = "ingredients" if ref_type == "ingredient" else "recipes"
        row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (ref_id,)).fetchone()
        if row is None:
            raise ApiError(422, "UNKNOWN_REFERENCE", f"{where} 引用了不存在的 {ref_type} id={ref_id}")
        parsed.append({"ref_type": ref_type, "ref_id": ref_id, "grams": grams})
    return parsed


def _check_version(row, expected: int, kind: str, rid: int):
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"{kind} id={rid} 不存在")
    if row["version"] != expected:
        raise ApiError(
            409, "VERSION_CONFLICT",
            f"{kind} id={rid} 当前版本为 {row['version']}，与所见版本 {expected} 冲突",
            details={"current_version": row["version"], "expected_version": expected},
        )


# ---------------------------------------------------------------- 序列化

def _ingredient_json(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "allergens": json.loads(row["allergens"]),
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _recipe_json(conn, row) -> dict:
    comps = conn.execute(
        "SELECT seq, ref_type, ref_id, grams FROM recipe_components WHERE recipe_id = ? ORDER BY seq",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "components": [
            {"seq": c["seq"], "type": c["ref_type"], "id": c["ref_id"], "grams": _to_num(Fraction(str(c["grams"])))}
            for c in comps
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------- 原料

def create_ingredient(conn, payload: dict) -> dict:
    name = _parse_name(payload)
    allergens = _parse_allergens(payload)
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO ingredients (name, allergens, version, created_at, updated_at) VALUES (?,?,1,?,?)",
            (name, json.dumps(allergens), now, now),
        )
        row = conn.execute("SELECT * FROM ingredients WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _ingredient_json(row)


def revise_ingredient(conn, ingredient_id: int, payload: dict) -> dict:
    name = _parse_name(payload)
    allergens = _parse_allergens(payload)
    expected = _parse_expected_version(payload, required=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
        _check_version(row, expected, "ingredient", ingredient_id)
        conn.execute(
            "UPDATE ingredients SET name=?, allergens=?, version=version+1, updated_at=? WHERE id=?",
            (name, json.dumps(allergens), _now(), ingredient_id),
        )
        row = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _ingredient_json(row)


def get_ingredient(conn, ingredient_id: int) -> dict:
    row = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"ingredient id={ingredient_id} 不存在")
    return _ingredient_json(row)


def list_ingredients(conn) -> list:
    rows = conn.execute("SELECT * FROM ingredients ORDER BY id").fetchall()
    return [_ingredient_json(r) for r in rows]


# ---------------------------------------------------------------- 配方

def create_recipe(conn, payload: dict) -> dict:
    name = _parse_name(payload)
    conn.execute("BEGIN IMMEDIATE")
    try:
        comps = _parse_components(conn, payload)
        now = _now()
        cur = conn.execute(
            "INSERT INTO recipes (name, version, created_at, updated_at) VALUES (?,1,?,?)",
            (name, now, now),
        )
        rid = cur.lastrowid
        for seq, c in enumerate(comps):
            conn.execute(
                "INSERT INTO recipe_components (recipe_id, seq, ref_type, ref_id, grams) VALUES (?,?,?,?,?)",
                (rid, seq, c["ref_type"], c["ref_id"], c["grams"]),
            )
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _recipe_json(conn, row)


def revise_recipe(conn, recipe_id: int, payload: dict) -> dict:
    name = _parse_name(payload)
    expected = _parse_expected_version(payload, required=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        _check_version(row, expected, "recipe", recipe_id)
        comps = _parse_components(conn, payload)  # 先全部校验通过才改稿
        conn.execute("DELETE FROM recipe_components WHERE recipe_id = ?", (recipe_id,))
        for seq, c in enumerate(comps):
            conn.execute(
                "INSERT INTO recipe_components (recipe_id, seq, ref_type, ref_id, grams) VALUES (?,?,?,?,?)",
                (recipe_id, seq, c["ref_type"], c["ref_id"], c["grams"]),
            )
        conn.execute(
            "UPDATE recipes SET name=?, version=version+1, updated_at=? WHERE id=?",
            (name, _now(), recipe_id),
        )
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _recipe_json(conn, row)


def get_recipe(conn, recipe_id: int) -> dict:
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"recipe id={recipe_id} 不存在")
    return _recipe_json(conn, row)


def list_recipes(conn) -> list:
    rows = conn.execute("SELECT * FROM recipes ORDER BY id").fetchall()
    return [_recipe_json(conn, r) for r in rows]


# ---------------------------------------------------------------- 发布

def _resolve(conn, recipe_id: int, stack: list, deps: dict):
    """递归解析配方。

    返回 (total: Fraction, items: [(ingredient_row, grams: Fraction, path: tuple)])。
    stack 为从根到当前的 [(recipe_id, name)]，用于循环检测与来源路径。
    """
    row = conn.execute("SELECT id, name, version FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise ApiError(422, "UNKNOWN_REFERENCE", f"引用了不存在的 recipe id={recipe_id}")
    if any(rid == recipe_id for rid, _ in stack):
        chain = " -> ".join([n for _, n in stack] + [row["name"]])
        raise ApiError(422, "CYCLE_DETECTED", f"配方依赖存在循环: {chain}")
    stack = stack + [(row["id"], row["name"])]
    deps["recipes"][row["id"]] = {"name": row["name"], "version": row["version"]}

    comps = conn.execute(
        "SELECT ref_type, ref_id, grams FROM recipe_components WHERE recipe_id = ? ORDER BY seq",
        (recipe_id,),
    ).fetchall()
    total = sum((Fraction(str(c["grams"])) for c in comps), Fraction(0))
    if total <= 0:
        raise ApiError(422, "ZERO_TOTAL", f"配方 '{row['name']}' (id={recipe_id}) 总克数为 0，无法发布")

    items = []
    for c in comps:
        grams = Fraction(str(c["grams"]))
        if c["ref_type"] == "ingredient":
            ing = conn.execute("SELECT * FROM ingredients WHERE id = ?", (c["ref_id"],)).fetchone()
            if ing is None:
                raise ApiError(422, "UNKNOWN_REFERENCE", f"引用了不存在的 ingredient id={c['ref_id']}")
            deps["ingredients"][ing["id"]] = {"name": ing["name"], "version": ing["version"]}
            items.append((ing, grams, tuple(n for _, n in stack)))
        else:
            sub_total, sub_items = _resolve(conn, c["ref_id"], stack, deps)
            factor = grams / sub_total  # 子配方按自身总克数折算
            items.extend((ing, amt * factor, path) for ing, amt, path in sub_items)
    return total, items


def _aggregate(items) -> dict:
    """按过敏原汇总原料克数与来源路径；同一原料多条路径合并计。"""
    allergens: dict = {}
    for ing, amount, path in items:
        for a in json.loads(ing["allergens"]):
            per_ing = allergens.setdefault(a, {})
            slot = per_ing.setdefault(ing["id"], {
                "ingredient_id": ing["id"],
                "name": ing["name"],
                "grams": Fraction(0),
                "paths": set(),
            })
            slot["grams"] += amount
            slot["paths"].add(path)

    out = {}
    for allergen in sorted(allergens):
        ings = sorted(allergens[allergen].values(), key=lambda s: s["name"])
        out[allergen] = {
            "total_grams": _to_num(sum((s["grams"] for s in ings), Fraction(0))),
            "ingredients": [
                {
                    "ingredient_id": s["ingredient_id"],
                    "name": s["name"],
                    "grams": _to_num(s["grams"]),
                    "paths": sorted(" > ".join(p) for p in s["paths"]),
                }
                for s in ings
            ],
        }
    return out


def publish_recipe(conn, recipe_id: int, payload: dict) -> dict:
    """发布：同一事务（一致视图）内递归校验、汇总并写入不可变标签。"""
    expected = _parse_expected_version(payload or {}, required=False)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"recipe id={recipe_id} 不存在")
        if expected is not None and row["version"] != expected:
            raise ApiError(
                409, "VERSION_CONFLICT",
                f"recipe id={recipe_id} 当前版本为 {row['version']}，与所见版本 {expected} 冲突",
                details={"current_version": row["version"], "expected_version": expected},
            )

        deps = {"recipes": {}, "ingredients": {}}
        total, items = _resolve(conn, recipe_id, [], deps)
        snapshot = {
            "recipe": {"id": row["id"], "name": row["name"], "version": row["version"]},
            "total_grams": _to_num(total),
            "allergens": _aggregate(items),
            "dependency_versions": {
                "recipes": sorted(
                    ({"id": rid, "name": d["name"], "version": d["version"]} for rid, d in deps["recipes"].items()),
                    key=lambda d: d["id"],
                ),
                "ingredients": sorted(
                    ({"id": iid, "name": d["name"], "version": d["version"]} for iid, d in deps["ingredients"].items()),
                    key=lambda d: d["id"],
                ),
            },
        }

        # 提交前复核所有依赖版本：发布期间若有变化则整体失败，不留半成品。
        for rid, d in deps["recipes"].items():
            cur = conn.execute("SELECT version FROM recipes WHERE id = ?", (rid,)).fetchone()
            if cur is None or cur["version"] != d["version"]:
                raise ApiError(409, "CONCURRENT_MODIFICATION", f"发布期间 recipe id={rid} 被修改，已放弃本次发布")
        for iid, d in deps["ingredients"].items():
            cur = conn.execute("SELECT version FROM ingredients WHERE id = ?", (iid,)).fetchone()
            if cur is None or cur["version"] != d["version"]:
                raise ApiError(409, "CONCURRENT_MODIFICATION", f"发布期间 ingredient id={iid} 被修改，已放弃本次发布")

        cur = conn.execute(
            "INSERT INTO labels (recipe_id, recipe_version, snapshot, created_at) VALUES (?,?,?,?)",
            (recipe_id, row["version"], json.dumps(snapshot, ensure_ascii=False), _now()),
        )
        label_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_label(conn, label_id)


# ---------------------------------------------------------------- 标签

def _label_json(row) -> dict:
    snapshot = json.loads(row["snapshot"])
    return {
        "id": row["id"],
        "recipe_id": row["recipe_id"],
        "recipe_version": row["recipe_version"],
        "created_at": row["created_at"],
        "total_grams": snapshot["total_grams"],
        "allergens": snapshot["allergens"],
        "dependency_versions": snapshot["dependency_versions"],
    }


def get_label(conn, label_id: int) -> dict:
    row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"label id={label_id} 不存在")
    return _label_json(row)


def list_labels(conn, recipe_id: int) -> list:
    row = conn.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"recipe id={recipe_id} 不存在")
    rows = conn.execute("SELECT * FROM labels WHERE recipe_id = ? ORDER BY id", (recipe_id,)).fetchall()
    return [_label_json(r) for r in rows]


def map_sqlite_error(exc: sqlite3.Error) -> ApiError:
    """把 SQLite 并发/约束错误映射为 API 错误。"""
    msg = str(exc)
    if isinstance(exc, sqlite3.OperationalError) and "lock" in msg.lower():
        return ApiError(409, "CONCURRENT_MODIFICATION", "数据库正被其他请求写入，请重试")
    return ApiError(409, "DB_CONSTRAINT", f"数据库约束拒绝: {msg}")
