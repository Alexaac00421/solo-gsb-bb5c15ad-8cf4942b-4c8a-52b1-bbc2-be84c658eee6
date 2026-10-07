#!/usr/bin/env bash
# HTTP 调用示例：从建原料到发布标签的完整流程。
# 前提：服务已启动（默认 http://localhost:8080）。
set -e
BASE="${BASE:-http://localhost:8080}"

echo "== 1. 新建原料草稿（记录过敏原） =="
curl -s -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"name": "面粉", "allergens": ["gluten"]}'
echo
curl -s -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"name": "鸡蛋", "allergens": ["egg"]}'
echo
curl -s -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"name": "黄油", "allergens": ["milk"]}'
echo

echo "== 2. 修订原料（必须带所见版本；版本不符返回 409 且不改稿） =="
curl -s -X PUT "$BASE/ingredients/1" -H 'Content-Type: application/json' \
  -d '{"name": "高筋面粉", "allergens": ["gluten"], "version": 1}'
echo
echo "-- 用过期版本再改，应 409 version_conflict --"
curl -s -X PUT "$BASE/ingredients/1" -H 'Content-Type: application/json' \
  -d '{"name": "x", "allergens": ["gluten"], "version": 1}'
echo

echo "== 3. 新建配方草稿（按克引用原料或子配方） =="
curl -s -X POST "$BASE/recipes" -H 'Content-Type: application/json' -d '{
  "name": "海绵胚",
  "components": [
    {"ingredient_id": 1, "grams": 500},
    {"ingredient_id": 2, "grams": 300}
  ]}'
echo
curl -s -X POST "$BASE/recipes" -H 'Content-Type: application/json' -d '{
  "name": "蛋糕",
  "components": [
    {"recipe_id": 1, "grams": 600},
    {"ingredient_id": 3, "grams": 200},
    {"ingredient_id": 1, "grams": 50}
  ]}'
echo

echo "== 4. 非法草稿：非正用量 / 不存在的引用（均拒绝且不改稿） =="
curl -s -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"name": "坏", "components": [{"ingredient_id": 1, "grams": 0}]}'
echo
curl -s -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"name": "坏", "components": [{"ingredient_id": 999, "grams": 10}]}'
echo

echo "== 5. 发布标签（带所见版本；递归展开、子配方按自身总克数折算） =="
curl -s -X POST "$BASE/recipes/2/publish" -H 'Content-Type: application/json' \
  -d '{"version": 1}'
echo

echo "== 6. 查询标签（不可变；后续修订不影响旧标签） =="
curl -s "$BASE/recipes/2/labels"
echo
curl -s "$BASE/labels/1"
echo
