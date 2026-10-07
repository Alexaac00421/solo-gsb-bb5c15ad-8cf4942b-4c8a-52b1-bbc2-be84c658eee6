"""业务逻辑：草稿修订（乐观锁）、发布（递归展开 + 一致性快照）。

错误约定：抛出 ApiError(status, code, message)，由 HTTP 层转成 JSON 响应。

关键不变式：
- 所有修订都要求客户端带上“所见版本”，在 BEGIN IMMEDIATE 事务内
  检查并递增，冲突/非正用量/不存在的引用一律拒绝且不落任何修改。
- 发布在单个 BEGIN IMMEDIATE 事务中完成：递归展开依赖、检测循环、
  校验总重、记录依赖版本快照，提交前再次校验所有依赖版本未变；
  任一步失败即 ROLLBACK，不留下半成品标签。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 工具

def _require_fields(body: dict, *fields: str) -> None:
    missing = [f for f in fields if f not in body]
    if missing:
        raise ApiError(400, "validation", f"缺少字段: {', '.join(missing)}")


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "validation", "name 必须是非空字符串")
    return name.strip()


def _check_allergens(allergens: Any) -> list[str]:
    if not isinstance(allergens, list) or not all(
        isinstance(a, str) and a.strip() for a in allergens
    ):
        raise ApiError(400, "validation", "allergens 必须是非空字符串数组")
    # 去重并保持顺序
    seen: dict[str, None] = {}
    for a in allergens:
        seen[a.strip()] = None
    return list(seen)


def _check_version(version: Any) -> int:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ApiError(400, "validation", "version 必须是 >= 1 的整数（客户端所见版本）")
    return version


def _check_grams(grams: Any) -> float:
    if not isinstance(grams, (int, float)) or isinstance(grams, bool):
        raise ApiError(422, "invalid_amount", "grams 必须是数字")
    if grams <= 0:
        raise ApiError(422, "invalid_amount", f"用量必须为正数，收到 {grams}")
    return float(grams)


def _ingredient_row(conn: sqlite3.Connection, ingredient_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"原料 {ingredient_id} 不存在")
    return row


def _recipe_row(conn: sqlite3.Connection, recipe_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"配方 {recipe_id} 不存在")
    return row


def _ingredient_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "allergens": json.loads(row["allergens"]),
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def _recipe_json(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    comps = conn.execute(
        "SELECT * FROM recipe_components WHERE recipe_id = ? ORDER BY seq",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "updated_at": row["updated_at"],
        "components": [
            {
                **({"ingredient_id": c["ingredient_id"]} if c["ingredient_id"] is not None
                   else {"recipe_id": c["sub_recipe_id"]}),
                "grams": c["grams"],
            }
            for c in comps
        ],
    }


# ---------------------------------------------------------------- 原料草稿

def create_ingredient(conn: sqlite3.Connection, body: dict) -> tuple[int, dict]:
    _require_fields(body, "name", "allergens")
    name = _check_name(body["name"])
    allergens = _check_allergens(body["allergens"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO ingredients (name, allergens) VALUES (?, ?)",
            (name, json.dumps(allergens, ensure_ascii=False)),
        )
        row = _ingredient_row(conn, cur.lastrowid)
        conn.execute("COMMIT")
        return 201, _ingredient_json(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_ingredient(conn: sqlite3.Connection, ingredient_id: int) -> tuple[int, dict]:
    return 200, _ingredient_json(_ingredient_row(conn, ingredient_id))


def update_ingredient(
    conn: sqlite3.Connection, ingredient_id: int, body: dict
) -> tuple[int, dict]:
    _require_fields(body, "name", "allergens", "version")
    name = _check_name(body["name"])
    allergens = _check_allergens(body["allergens"])
    version = _check_version(body["version"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _ingredient_row(conn, ingredient_id)
        if row["version"] != version:
            raise ApiError(
                409, "version_conflict",
                f"原料 {ingredient_id} 当前版本为 {row['version']}，提交版本为 {version}",
            )
        conn.execute(
            "UPDATE ingredients SET name = ?, allergens = ?, version = version + 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (name, json.dumps(allergens, ensure_ascii=False), ingredient_id),
        )
        out = _ingredient_json(_ingredient_row(conn, ingredient_id))
        conn.execute("COMMIT")
        return 200, out
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 配方草稿

def _validate_components(conn: sqlite3.Connection, components: Any) -> list[dict]:
    """校验成分列表：非正用量、不存在的引用在此拒绝（调用方须在事务内）。"""
    if not isinstance(components, list):
        raise ApiError(400, "validation", "components 必须是数组")
    checked = []
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            raise ApiError(400, "validation", f"components[{i}] 必须是对象")
        has_ing = "ingredient_id" in comp
        has_rec = "recipe_id" in comp
        if has_ing == has_rec:
            raise ApiError(
                400, "validation",
                f"components[{i}] 必须且只能包含 ingredient_id 或 recipe_id 之一",
            )
        grams = _check_grams(comp.get("grams"))
        if has_ing:
            ref = comp["ingredient_id"]
            if not isinstance(ref, int) or isinstance(ref, bool):
                raise ApiError(400, "validation", f"components[{i}].ingredient_id 必须是整数")
            if conn.execute(
                "SELECT 1 FROM ingredients WHERE id = ?", (ref,)
            ).fetchone() is None:
                raise ApiError(422, "missing_reference", f"引用的原料 {ref} 不存在")
            checked.append({"ingredient_id": ref, "sub_recipe_id": None, "grams": grams})
        else:
            ref = comp["recipe_id"]
            if not isinstance(ref, int) or isinstance(ref, bool):
                raise ApiError(400, "validation", f"components[{i}].recipe_id 必须是整数")
            if conn.execute(
                "SELECT 1 FROM recipes WHERE id = ?", (ref,)
            ).fetchone() is None:
                raise ApiError(422, "missing_reference", f"引用的配方 {ref} 不存在")
            checked.append({"ingredient_id": None, "sub_recipe_id": ref, "grams": grams})
    return checked


def _replace_components(
    conn: sqlite3.Connection, recipe_id: int, components: list[dict]
) -> None:
    conn.execute("DELETE FROM recipe_components WHERE recipe_id = ?", (recipe_id,))
    conn.executemany(
        "INSERT INTO recipe_components (recipe_id, seq, ingredient_id, sub_recipe_id, grams) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (recipe_id, seq, c["ingredient_id"], c["sub_recipe_id"], c["grams"])
            for seq, c in enumerate(components)
        ],
    )


def create_recipe(conn: sqlite3.Connection, body: dict) -> tuple[int, dict]:
    _require_fields(body, "name", "components")
    name = _check_name(body["name"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        components = _validate_components(conn, body["components"])
        cur = conn.execute("INSERT INTO recipes (name) VALUES (?)", (name,))
        recipe_id = cur.lastrowid
        _replace_components(conn, recipe_id, components)
        out = _recipe_json(conn, _recipe_row(conn, recipe_id))
        conn.execute("COMMIT")
        return 201, out
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_recipe(conn: sqlite3.Connection, recipe_id: int) -> tuple[int, dict]:
    return 200, _recipe_json(conn, _recipe_row(conn, recipe_id))


def update_recipe(
    conn: sqlite3.Connection, recipe_id: int, body: dict
) -> tuple[int, dict]:
    _require_fields(body, "name", "components", "version")
    name = _check_name(body["name"])
    version = _check_version(body["version"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _recipe_row(conn, recipe_id)
        if row["version"] != version:
            raise ApiError(
                409, "version_conflict",
                f"配方 {recipe_id} 当前版本为 {row['version']}，提交版本为 {version}",
            )
        components = _validate_components(conn, body["components"])
        conn.execute(
            "UPDATE recipes SET name = ?, version = version + 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (name, recipe_id),
        )
        _replace_components(conn, recipe_id, components)
        out = _recipe_json(conn, _recipe_row(conn, recipe_id))
        conn.execute("COMMIT")
        return 200, out
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 发布

class _Expansion:
    """递归展开结果：每个原料的累计克数与出现路径，以及依赖版本快照。"""

    def __init__(self) -> None:
        # ingredient_id -> {"grams": float, "paths": [{"path": [...], "grams": float}]}
        self.ingredients: dict[int, dict] = {}
        self.recipe_versions: dict[int, int] = {}
        self.ingredient_versions: dict[int, int] = {}


def _expand_recipe(
    conn: sqlite3.Connection,
    recipe_id: int,
    scale: float,
    stack: list[int],
    names: list[str],
    out: _Expansion,
) -> float:
    """展开配方，返回该配方自身一批的总克数（scale=1 时即为配方总重）。

    scale 为相对该配方自身一批的缩放系数；子配方按 used/total 折算。
    """
    if recipe_id in stack:
        cycle = [str(r) for r in stack[stack.index(recipe_id):]] + [str(recipe_id)]
        raise ApiError(
            422, "cycle_detected",
            "检测到循环引用: " + " -> ".join(cycle),
        )
    row = _recipe_row(conn, recipe_id)
    out.recipe_versions[recipe_id] = row["version"]
    comps = conn.execute(
        "SELECT * FROM recipe_components WHERE recipe_id = ? ORDER BY seq",
        (recipe_id,),
    ).fetchall()
    total = sum(c["grams"] for c in comps)
    if total <= 0:
        raise ApiError(
            422, "zero_total_weight",
            f"配方 {recipe_id}（{row['name']}）总克数为 0，无法发布",
        )

    stack.append(recipe_id)
    names.append(f"{row['name']}(recipe:{recipe_id})")
    try:
        for c in comps:
            if c["ingredient_id"] is not None:
                ing = _ingredient_row(conn, c["ingredient_id"])
                out.ingredient_versions[ing["id"]] = ing["version"]
                grams = c["grams"] * scale
                entry = out.ingredients.setdefault(ing["id"], {"grams": 0.0, "paths": []})
                entry["grams"] += grams
                entry["paths"].append({
                    "path": names + [f"{ing['name']}(ingredient:{ing['id']})"],
                    "grams": grams,
                })
            else:
                sub_total = _recipe_total(conn, c["sub_recipe_id"])
                # 子配方用量按其自身总克数折算
                _expand_recipe(
                    conn, c["sub_recipe_id"],
                    scale * c["grams"] / sub_total,
                    stack, names, out,
                )
    finally:
        stack.pop()
        names.pop()
    return total


def _recipe_total(conn: sqlite3.Connection, recipe_id: int) -> float:
    comps = conn.execute(
        "SELECT grams FROM recipe_components WHERE recipe_id = ?", (recipe_id,)
    ).fetchall()
    total = sum(c["grams"] for c in comps)
    if total <= 0:
        raise ApiError(
            422, "zero_total_weight",
            f"配方 {recipe_id} 总克数为 0，无法作为子配方折算",
        )
    return total


def publish_label(conn: sqlite3.Connection, recipe_id: int, body: dict) -> tuple[int, dict]:
    _require_fields(body, "version")
    version = _check_version(body["version"])
    # BEGIN IMMEDIATE：整个发布期间持有写锁，其他写事务无法插入，
    # 因此读到的是同一一致视图；提交前再显式复核一次依赖版本。
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _recipe_row(conn, recipe_id)
        if row["version"] != version:
            raise ApiError(
                409, "version_conflict",
                f"配方 {recipe_id} 当前版本为 {row['version']}，发布请求版本为 {version}",
            )

        expansion = _Expansion()
        total = _expand_recipe(conn, recipe_id, 1.0, [], [], expansion)

        # 提交前复核：所有依赖（含本配方）版本必须与展开时一致，
        # 否则视为发布期间依赖被改动，整体失败回滚。
        stale = []
        for rid, ver in expansion.recipe_versions.items():
            cur = conn.execute("SELECT version FROM recipes WHERE id = ?", (rid,)).fetchone()
            if cur is None or cur["version"] != ver:
                stale.append(f"recipe:{rid}")
        for iid, ver in expansion.ingredient_versions.items():
            cur = conn.execute("SELECT version FROM ingredients WHERE id = ?", (iid,)).fetchone()
            if cur is None or cur["version"] != ver:
                stale.append(f"ingredient:{iid}")
        if stale:
            raise ApiError(
                409, "stale_dependency",
                "发布期间依赖发生变化: " + ", ".join(stale),
            )

        # 汇总：过敏原 -> 原料克数（同一原料多条路径合计）与来源路径
        allergens: dict[str, dict] = {}
        for iid, data in expansion.ingredients.items():
            ing = _ingredient_row(conn, iid)
            for allergen in json.loads(ing["allergens"]):
                bucket = allergens.setdefault(allergen, {"total_grams": 0.0, "ingredients": {}})
                bucket["total_grams"] += data["grams"]
                slot = bucket["ingredients"].setdefault(iid, {
                    "ingredient_id": iid,
                    "name": ing["name"],
                    "grams": 0.0,
                    "paths": [],
                })
                slot["grams"] += data["grams"]
                slot["paths"].extend(
                    {"path": p["path"], "grams": round(p["grams"], 6)}
                    for p in data["paths"]
                )

        content = {
            "recipe_id": recipe_id,
            "recipe_name": row["name"],
            "recipe_version": version,
            "total_grams": total,
            "allergens": {
                allergen: {
                    "total_grams": round(bucket["total_grams"], 6),
                    "ingredients": sorted(
                        (
                            {**slot, "grams": round(slot["grams"], 6)}
                            for slot in bucket["ingredients"].values()
                        ),
                        key=lambda s: s["ingredient_id"],
                    ),
                }
                for allergen, bucket in sorted(allergens.items())
            },
        }
        deps = {
            "recipes": {str(k): v for k, v in sorted(expansion.recipe_versions.items())},
            "ingredients": {str(k): v for k, v in sorted(expansion.ingredient_versions.items())},
        }
        cur = conn.execute(
            "INSERT INTO labels (recipe_id, recipe_version, total_grams, content, deps) "
            "VALUES (?, ?, ?, ?, ?)",
            (recipe_id, version, total,
             json.dumps(content, ensure_ascii=False),
             json.dumps(deps, ensure_ascii=False)),
        )
        label = _label_json(conn.execute("SELECT * FROM labels WHERE id = ?",
                                         (cur.lastrowid,)).fetchone())
        conn.execute("COMMIT")
        return 201, label
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _label_json(row: sqlite3.Row) -> dict:
    content = json.loads(row["content"])
    content["label_id"] = row["id"]
    content["published_at"] = row["published_at"]
    content["dependencies"] = json.loads(row["deps"])
    return content


def get_label(conn: sqlite3.Connection, label_id: int) -> tuple[int, dict]:
    row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"标签 {label_id} 不存在")
    return 200, _label_json(row)


def list_labels(conn: sqlite3.Connection, recipe_id: int) -> tuple[int, dict]:
    _recipe_row(conn, recipe_id)
    rows = conn.execute(
        "SELECT * FROM labels WHERE recipe_id = ? ORDER BY id", (recipe_id,)
    ).fetchall()
    return 200, {"recipe_id": recipe_id, "labels": [_label_json(r) for r in rows]}
