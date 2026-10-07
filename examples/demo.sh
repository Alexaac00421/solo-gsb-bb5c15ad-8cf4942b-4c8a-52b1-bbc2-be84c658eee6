#!/bin/sh
# HTTP 调用示例：从建稿到发布的完整流程。
# 用法：先启动服务（docker compose up --build），再执行  sh examples/demo.sh
set -e
BASE="${BASE:-http://localhost:8000}"
J='Content-Type: application/json'

echo "== 0. 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1. 创建原料草稿（记录过敏原）=="
curl -s -X POST "$BASE/ingredients" -H "$J" \
  -d '{"name":"面粉","allergens":["gluten"]}'; echo
curl -s -X POST "$BASE/ingredients" -H "$J" \
  -d '{"name":"黄油","allergens":["milk"]}'; echo
curl -s -X POST "$BASE/ingredients" -H "$J" \
  -d '{"name":"杏仁粉","allergens":["tree_nut"]}'; echo

echo "== 2. 修订须带所见版本：版本不符 -> 409 且不改稿 =="
curl -s -X PUT "$BASE/ingredients/1" -H "$J" \
  -d '{"name":"面粉","allergens":["gluten"],"expected_version":99}'; echo

echo "== 3. 非正用量 -> 422；不存在的引用 -> 422（均不改稿）=="
curl -s -X POST "$BASE/recipes" -H "$J" \
  -d '{"name":"坏配方","components":[{"type":"ingredient","id":1,"grams":0}]}'; echo
curl -s -X POST "$BASE/recipes" -H "$J" \
  -d '{"name":"坏配方","components":[{"type":"ingredient","id":999,"grams":10}]}'; echo

echo "== 4. 创建子配方与主配方（按克引用原料/配方）=="
curl -s -X POST "$BASE/recipes" -H "$J" -d '{"name":"海绵蛋糕","components":[
  {"type":"ingredient","id":1,"grams":50},
  {"type":"ingredient","id":2,"grams":50}]}'; echo
curl -s -X POST "$BASE/recipes" -H "$J" -d '{"name":"蛋糕","components":[
  {"type":"ingredient","id":1,"grams":100},
  {"type":"ingredient","id":3,"grams":30},
  {"type":"recipe","id":1,"grams":200}]}'; echo

echo "== 5. 发布：子配方按自身总克数折算，同一原料多路径合计 =="
curl -s -X POST "$BASE/recipes/2/publish" -H "$J" -d '{"expected_version":1}'; echo

echo "== 6. 发布后修订原料，旧标签保持不变 =="
curl -s -X PUT "$BASE/ingredients/1" -H "$J" \
  -d '{"name":"面粉","allergens":["gluten","soy"],"expected_version":1}' > /dev/null
curl -s "$BASE/labels/1"; echo

echo "== 7. 再次发布生成新标签（记录新依赖版本），旧标签仍在 =="
curl -s -X PUT "$BASE/recipes/2" -H "$J" -d '{"name":"蛋糕","expected_version":1,"components":[
  {"type":"ingredient","id":1,"grams":100},
  {"type":"ingredient","id":3,"grams":30},
  {"type":"recipe","id":1,"grams":200}]}' > /dev/null
curl -s -X POST "$BASE/recipes/2/publish" -H "$J" -d '{"expected_version":2}' > /dev/null
curl -s "$BASE/recipes/2/labels"; echo

echo "== 8. 循环依赖与零总重 -> 发布失败且不留半成品 =="
curl -s -X POST "$BASE/recipes" -H "$J" -d '{"name":"甲","components":[]}' > /dev/null
curl -s -X POST "$BASE/recipes" -H "$J" \
  -d '{"name":"乙","components":[{"type":"recipe","id":3,"grams":10}]}' > /dev/null
curl -s -X PUT "$BASE/recipes/3" -H "$J" \
  -d '{"name":"甲","expected_version":1,"components":[{"type":"recipe","id":4,"grams":10}]}' > /dev/null
curl -s -X POST "$BASE/recipes/3/publish"; echo
curl -s -X POST "$BASE/recipes" -H "$J" -d '{"name":"空配方","components":[]}' > /dev/null
curl -s -X POST "$BASE/recipes/5/publish"; echo
