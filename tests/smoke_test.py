"""端到端冒烟测试：启动真实 HTTP 服务，覆盖需求全部规则。

运行：python3 tests/smoke_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = 18090
BASE = f"http://127.0.0.1:{PORT}"

passed = failed = 0


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def main():
    db_path = os.path.join(tempfile.mkdtemp(), "test.db")
    env = {**os.environ, "DB_PATH": db_path, "PORT": str(PORT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
    )
    try:
        for _ in range(50):
            try:
                s, _ = call("GET", "/healthz")
                if s == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            print("服务未能启动")
            sys.exit(1)

        print("== 原料草稿 ==")
        s, flour = call("POST", "/ingredients", {"name": "面粉", "allergens": ["gluten"]})
        check("创建原料 201", s == 201 and flour["version"] == 1, (s, flour))
        s, egg = call("POST", "/ingredients", {"name": "鸡蛋", "allergens": ["egg"]})
        s, milk = call("POST", "/ingredients", {"name": "牛奶", "allergens": ["milk"]})
        s, butter = call("POST", "/ingredients", {"name": "黄油", "allergens": ["milk"]})
        s, sugar = call("POST", "/ingredients", {"name": "糖", "allergens": []})

        s, r = call("PUT", f"/ingredients/{flour['id']}",
                    {"name": "高筋面粉", "allergens": ["gluten"], "version": 1})
        check("修订原料成功 version->2", s == 200 and r["version"] == 2, (s, r))
        flour = r
        s, r = call("PUT", f"/ingredients/{flour['id']}",
                    {"name": "X", "allergens": ["gluten"], "version": 1})
        check("过期版本修订被拒 409", s == 409 and r["error"]["code"] == "version_conflict", (s, r))
        s, r2 = call("GET", f"/ingredients/{flour['id']}")
        check("冲突后草稿未被改动", r2["name"] == "高筋面粉" and r2["version"] == 2, r2)
        s, r = call("PUT", "/ingredients/9999",
                    {"name": "X", "allergens": [], "version": 1})
        check("修订不存在原料 404", s == 404, (s, r))

        print("== 配方草稿 ==")
        # 海绵胚：面粉500 + 鸡蛋300 + 糖200 = 1000g
        s, sponge = call("POST", "/recipes", {"name": "海绵胚", "components": [
            {"ingredient_id": flour["id"], "grams": 500},
            {"ingredient_id": egg["id"], "grams": 300},
            {"ingredient_id": sugar["id"], "grams": 200},
        ]})
        check("创建配方 201", s == 201 and sponge["version"] == 1, (s, sponge))
        # 奶油霜：黄油400 + 牛奶100 = 500g
        s, cream = call("POST", "/recipes", {"name": "奶油霜", "components": [
            {"ingredient_id": butter["id"], "grams": 400},
            {"ingredient_id": milk["id"], "grams": 100},
        ]})
        # 蛋糕：海绵胚600g + 奶油霜250g + 面粉50g（面粉同时经两条路径出现）
        s, cake = call("POST", "/recipes", {"name": "蛋糕", "components": [
            {"recipe_id": sponge["id"], "grams": 600},
            {"recipe_id": cream["id"], "grams": 250},
            {"ingredient_id": flour["id"], "grams": 50},
        ]})
        check("嵌套配方创建成功", s == 201, (s, cake))

        s, r = call("POST", "/recipes", {"name": "坏配方", "components": [
            {"ingredient_id": flour["id"], "grams": 0}]})
        check("零用量被拒 422", s == 422 and r["error"]["code"] == "invalid_amount", (s, r))
        s, r = call("POST", "/recipes", {"name": "坏配方2", "components": [
            {"ingredient_id": flour["id"], "grams": -5}]})
        check("负用量被拒 422", s == 422, (s, r))
        s, r = call("POST", "/recipes", {"name": "坏配方3", "components": [
            {"ingredient_id": 9999, "grams": 10}]})
        check("引用不存在原料被拒 422", s == 422 and r["error"]["code"] == "missing_reference", (s, r))
        s, r = call("POST", "/recipes", {"name": "坏配方4", "components": [
            {"recipe_id": 9999, "grams": 10}]})
        check("引用不存在配方被拒 422", s == 422 and r["error"]["code"] == "missing_reference", (s, r))

        s, r = call("PUT", f"/recipes/{cake['id']}",
                    {"name": "蛋糕", "version": 99, "components": cake["components"]})
        check("配方版本冲突 409", s == 409 and r["error"]["code"] == "version_conflict", (s, r))

        print("== 循环与零总重 ==")
        s, ra = call("POST", "/recipes", {"name": "A", "components": [
            {"ingredient_id": sugar["id"], "grams": 10}]})
        s, rb = call("POST", "/recipes", {"name": "B", "components": [
            {"recipe_id": ra["id"], "grams": 10}]})
        s, ra2 = call("PUT", f"/recipes/{ra['id']}", {"name": "A", "version": 1,
                      "components": [{"recipe_id": rb["id"], "grams": 10}]})
        check("允许保存成环草稿（发布时才失败）", s == 200, (s, ra2))
        s, r = call("POST", f"/recipes/{ra['id']}/publish", {"version": 2})
        check("循环引用发布失败 422", s == 422 and r["error"]["code"] == "cycle_detected", (s, r))
        s, empty = call("POST", "/recipes", {"name": "空配方", "components": []})
        s, r = call("POST", f"/recipes/{empty['id']}/publish", {"version": 1})
        check("零总重发布失败 422", s == 422 and r["error"]["code"] == "zero_total_weight", (s, r))
        s, r = call("GET", f"/recipes/{ra['id']}/labels")
        check("失败发布不留标签", s == 200 and r["labels"] == [], r)

        print("== 发布与汇总 ==")
        s, label = call("POST", f"/recipes/{cake['id']}/publish", {"version": 1})
        check("发布成功 201", s == 201, (s, label))
        # 折算：海绵胚 600/1000=0.6 -> 面粉300 鸡蛋180 糖120
        #       奶油霜 250/500=0.5 -> 黄油200 牛奶50
        #       直接面粉 50 -> 面粉合计 350
        al = label["allergens"]
        check("总克数 900", label["total_grams"] == 900, label["total_grams"])
        check("gluten 总量 350（多路径合计）", al["gluten"]["total_grams"] == 350, al["gluten"])
        flour_slot = al["gluten"]["ingredients"][0]
        check("面粉经 2 条路径", len(flour_slot["paths"]) == 2, flour_slot)
        check("egg 总量 180", al["egg"]["total_grams"] == 180, al.get("egg"))
        check("milk 总量 250（黄油200+牛奶50）", al["milk"]["total_grams"] == 250, al.get("milk"))
        check("无糖则无空过敏原条目", "" not in al and len(al) == 3, list(al))
        deps = label["dependencies"]
        check("依赖版本快照含子配方与原料",
              deps["recipes"] == {str(cake["id"]): 1, str(sponge["id"]): 1, str(cream["id"]): 1}
              and str(flour["id"]) in deps["ingredients"], deps)

        s, r = call("POST", f"/recipes/{cake['id']}/publish", {"version": 1})
        check("发布不改草稿版本，同版本可再发布为新标签",
              s == 201 and r["label_id"] != label["label_id"], (s, r))

        print("== 标签不可变 ==")
        label_id = label["label_id"]
        s, before = call("GET", f"/labels/{label_id}")
        # 修订原料与配方后，旧标签必须原样保留
        s, flour3 = call("PUT", f"/ingredients/{flour['id']}",
                         {"name": "全麦面粉", "allergens": ["gluten", "nuts"], "version": 2})
        check("发布后仍可修订原料", s == 200 and flour3["version"] == 3, (s, flour3))
        s, after = call("GET", f"/labels/{label_id}")
        check("旧标签内容不随修订改变", before == after, (before, after))
        # 用新依赖再次发布（蛋糕版本仍为 1，因为没改过蛋糕本身）
        s, label2 = call("POST", f"/recipes/{cake['id']}/publish", {"version": 1})
        check("再次发布生成新标签", s == 201 and label2["label_id"] != label_id, (s, label2))
        check("新标签依赖快照反映原料新版本",
              label2["dependencies"]["ingredients"][str(flour["id"])] == 3, label2["dependencies"])
        check("新标签含 nuts", "nuts" in label2["allergens"], list(label2["allergens"]))
        s, labels = call("GET", f"/recipes/{cake['id']}/labels")
        check("标签列表累积", s == 200 and len(labels["labels"]) >= 2, labels)

        print("== 发布期间依赖变化 ==")
        # 直接连库模拟并发写：发布事务持有写锁时外部写会被阻塞/失败，
        # 这里验证复核逻辑——在事务外改动依赖后，用过期版本发布必失败。
        s, cake2 = call("PUT", f"/recipes/{cake['id']}", {"name": "蛋糕", "version": 1,
                        "components": cake["components"]})
        check("修订蛋糕 version->2", s == 200 and cake2["version"] == 2, (s, cake2))
        s, r = call("POST", f"/recipes/{cake['id']}/publish", {"version": 1})
        check("用过期所见版本发布被拒 409", s == 409 and r["error"]["code"] == "version_conflict", (s, r))
        s, r = call("GET", f"/recipes/{cake['id']}/labels")
        check("失败发布不留半成品（仍只有此前 3 个标签）",
              s == 200 and len(r["labels"]) == 3, r)

        print(f"\n通过 {passed}，失败 {failed}")
        sys.exit(1 if failed else 0)
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
