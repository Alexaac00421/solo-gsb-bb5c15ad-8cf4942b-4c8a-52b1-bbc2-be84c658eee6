"""业务规则测试：版本冲突、非正用量、未知引用、循环、零总重、折算汇总、标签不可变。"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db, service


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    # ------------------------------------------------ 辅助

    def mk_ingredient(self, name, allergens):
        return service.create_ingredient(self.conn, {"name": name, "allergens": allergens})

    def mk_recipe(self, name, components):
        return service.create_recipe(self.conn, {"name": name, "components": components})

    def comp(self, type_, id_, grams):
        return {"type": type_, "id": id_, "grams": grams}

    # ------------------------------------------------ 修订规则

    def test_version_conflict_rejected_and_draft_unchanged(self):
        ing = self.mk_ingredient("面粉", ["gluten"])
        with self.assertRaises(service.ApiError) as ctx:
            service.revise_ingredient(self.conn, ing["id"], {
                "name": "面粉X", "allergens": [], "expected_version": 99,
            })
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")
        after = service.get_ingredient(self.conn, ing["id"])
        self.assertEqual(after["name"], "面粉")       # 未改稿
        self.assertEqual(after["version"], 1)

    def test_missing_expected_version_rejected(self):
        ing = self.mk_ingredient("面粉", ["gluten"])
        with self.assertRaises(service.ApiError) as ctx:
            service.revise_ingredient(self.conn, ing["id"], {"name": "x", "allergens": []})
        self.assertEqual(ctx.exception.code, "VERSION_REQUIRED")

    def test_non_positive_amount_rejected_and_draft_unchanged(self):
        ing = self.mk_ingredient("面粉", ["gluten"])
        r = self.mk_recipe("面包", [self.comp("ingredient", ing["id"], 100)])
        for bad in (0, -5, "abc", None, True):
            with self.assertRaises(service.ApiError) as ctx:
                service.revise_recipe(self.conn, r["id"], {
                    "name": "面包", "expected_version": 1,
                    "components": [self.comp("ingredient", ing["id"], bad)],
                })
            self.assertEqual(ctx.exception.code, "NON_POSITIVE_AMOUNT", msg=f"grams={bad!r}")
        after = service.get_recipe(self.conn, r["id"])
        self.assertEqual(after["version"], 1)          # 未改稿
        self.assertEqual(after["components"][0]["grams"], 100)

    def test_unknown_reference_rejected_and_draft_unchanged(self):
        ing = self.mk_ingredient("面粉", ["gluten"])
        r = self.mk_recipe("面包", [self.comp("ingredient", ing["id"], 100)])
        with self.assertRaises(service.ApiError) as ctx:
            service.revise_recipe(self.conn, r["id"], {
                "name": "面包", "expected_version": 1,
                "components": [self.comp("ingredient", 9999, 100)],
            })
        self.assertEqual(ctx.exception.code, "UNKNOWN_REFERENCE")
        self.assertEqual(service.get_recipe(self.conn, r["id"])["version"], 1)

    def test_revision_success_bumps_version(self):
        ing = self.mk_ingredient("面粉", ["gluten"])
        r = self.mk_recipe("面包", [self.comp("ingredient", ing["id"], 100)])
        r2 = service.revise_recipe(self.conn, r["id"], {
            "name": "面包", "expected_version": 1,
            "components": [self.comp("ingredient", ing["id"], 250)],
        })
        self.assertEqual(r2["version"], 2)
        self.assertEqual(r2["components"][0]["grams"], 250)

    # ------------------------------------------------ 发布规则

    def test_publish_aggregates_and_scales_subrecipe(self):
        flour = self.mk_ingredient("面粉", ["gluten"])
        butter = self.mk_ingredient("黄油", ["milk"])
        # 海绵蛋糕：面粉 50 + 黄油 50，总 100g
        sponge = self.mk_recipe("海绵蛋糕", [
            self.comp("ingredient", flour["id"], 50),
            self.comp("ingredient", butter["id"], 50),
        ])
        # 蛋糕：面粉 100 + 海绵蛋糕 200（折算系数 200/100 = 2）
        cake = self.mk_recipe("蛋糕", [
            self.comp("ingredient", flour["id"], 100),
            self.comp("recipe", sponge["id"], 200),
        ])
        label = service.publish_recipe(self.conn, cake["id"], {"expected_version": 1})
        self.assertEqual(label["recipe_version"], 1)
        self.assertEqual(label["total_grams"], 300)
        gluten = label["allergens"]["gluten"]
        # 面粉经两条路径：直接 100 + 海绵蛋糕折算 100，合计 200
        self.assertEqual(gluten["total_grams"], 200)
        flour_entry = gluten["ingredients"][0]
        self.assertEqual(flour_entry["grams"], 200)
        self.assertEqual(flour_entry["paths"], ["蛋糕", "蛋糕 > 海绵蛋糕"])
        milk = label["allergens"]["milk"]
        self.assertEqual(milk["total_grams"], 100)
        self.assertEqual(milk["ingredients"][0]["paths"], ["蛋糕 > 海绵蛋糕"])
        # 依赖版本被记录
        dv = label["dependency_versions"]
        self.assertEqual({d["id"] for d in dv["recipes"]}, {cake["id"], sponge["id"]})
        self.assertEqual({d["id"] for d in dv["ingredients"]}, {flour["id"], butter["id"]})

    def test_publish_cycle_fails(self):
        a = self.mk_recipe("A", [])
        b = self.mk_recipe("B", [self.comp("recipe", a["id"], 10)])
        service.revise_recipe(self.conn, a["id"], {
            "name": "A", "expected_version": 1,
            "components": [self.comp("recipe", b["id"], 10)],
        })
        with self.assertRaises(service.ApiError) as ctx:
            service.publish_recipe(self.conn, a["id"], {})
        self.assertEqual(ctx.exception.code, "CYCLE_DETECTED")
        self.assertEqual(service.list_labels(self.conn, a["id"]), [])  # 无半成品

    def test_publish_zero_total_fails(self):
        r = self.mk_recipe("空配方", [])
        with self.assertRaises(service.ApiError) as ctx:
            service.publish_recipe(self.conn, r["id"], {})
        self.assertEqual(ctx.exception.code, "ZERO_TOTAL")
        self.assertEqual(service.list_labels(self.conn, r["id"]), [])

    def test_publish_version_conflict(self):
        ing = self.mk_ingredient("面粉", ["gluten"])
        r = self.mk_recipe("面包", [self.comp("ingredient", ing["id"], 100)])
        with self.assertRaises(service.ApiError) as ctx:
            service.publish_recipe(self.conn, r["id"], {"expected_version": 7})
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")

    def test_label_immutable_and_old_label_survives_revision(self):
        flour = self.mk_ingredient("面粉", ["gluten"])
        r = self.mk_recipe("面包", [self.comp("ingredient", flour["id"], 100)])
        label1 = service.publish_recipe(self.conn, r["id"], {})
        # 修订原料与配方后，旧标签不变
        service.revise_ingredient(self.conn, flour["id"], {
            "name": "面粉", "allergens": ["gluten", "soy"], "expected_version": 1,
        })
        service.revise_recipe(self.conn, r["id"], {
            "name": "面包", "expected_version": 1,
            "components": [self.comp("ingredient", flour["id"], 500)],
        })
        again = service.get_label(self.conn, label1["id"])
        self.assertEqual(again, label1)
        self.assertEqual(again["allergens"]["gluten"]["total_grams"], 100)
        self.assertNotIn("soy", again["allergens"])
        # 数据库层面禁止 UPDATE/DELETE
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE labels SET snapshot='{}' WHERE id=?", (label1["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM labels WHERE id=?", (label1["id"],))
        # 新发布生成新标签，记录新版本
        label2 = service.publish_recipe(self.conn, r["id"], {})
        self.assertEqual(label2["recipe_version"], 2)
        self.assertEqual(label2["allergens"]["gluten"]["total_grams"], 500)
        self.assertIn("soy", label2["allergens"])
        self.assertEqual(len(service.list_labels(self.conn, r["id"])), 2)

    def test_publish_dependency_change_during_publish_fails(self):
        """发布事务内依赖版本被改 -> 整体失败，不留标签。"""
        flour = self.mk_ingredient("面粉", ["gluten"])
        r = self.mk_recipe("面包", [self.comp("ingredient", flour["id"], 100)])

        real_resolve = service._resolve
        state = {"done": False}
        other = db.connect(self.tmp.name)

        def sneaky_resolve(conn, recipe_id, stack, deps):
            result = real_resolve(conn, recipe_id, stack, deps)
            if not state["done"]:
                state["done"] = True
                # 模拟另一连接在发布期间改了原料（绕过本连接的 IMMEDIATE 锁需直接改行）
                conn.execute("SAVEPOINT sneak")
                conn.execute("UPDATE ingredients SET version=version+1 WHERE id=?", (flour["id"],))
                conn.execute("RELEASE sneak")
            return result

        service._resolve = sneaky_resolve
        try:
            with self.assertRaises(service.ApiError) as ctx:
                service.publish_recipe(self.conn, r["id"], {})
            self.assertEqual(ctx.exception.code, "CONCURRENT_MODIFICATION")
        finally:
            service._resolve = real_resolve
            other.close()
        self.assertEqual(service.list_labels(self.conn, r["id"]), [])  # 无半成品


if __name__ == "__main__":
    unittest.main()
