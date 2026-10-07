# 中央厨房配方过敏原标签服务

用 Python（仅标准库）+ SQLite 实现的配方过敏原标签服务，对外提供 HTTP API。

- **原料草稿**：记录名称与过敏原列表。
- **配方草稿**：按克数引用原料或其他配方（可嵌套）。
- **乐观锁修订**：每次修订必须携带所见版本 `expected_version`；版本冲突（409）、非正用量（422）、不存在的引用（422）一律拒绝且不改稿。
- **发布**：在同一事务（一致视图）内递归检查依赖——循环依赖、零总重整次失败且不留半成品；子配方用量按其自身总克数折算；按过敏原汇总每种原料的克数与来源路径，同一原料经多条路径出现自动合计。
- **不可变标签**：发布成功生成不可变标签（数据库触发器禁止 UPDATE/DELETE），并记录发布时所用全部依赖的版本；后续修订不影响旧标签；发布期间依赖被修改则整次发布失败回滚。

## 快速开始

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**

容器启动时会自动执行 `pip install -r requirements.txt`（安装依赖，当前仅标准库、为空文件）并初始化 SQLite 数据库（建表、WAL 模式、不可变标签触发器）。

一条命令跑完整调用示例：

```bash
sh examples/demo.sh
```

本地开发（不用 Docker）：

```bash
python3 -m app.server          # 启动并自动初始化数据库
python3 -m unittest discover -s tests -v   # 运行测试
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8000` | HTTP 监听端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `DB_PATH` | `/data/app.db` | SQLite 文件路径（Compose 中挂载卷 `allergen-data` 持久化） |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/ingredients` | 创建原料草稿 `{name, allergens[]}` |
| GET | `/ingredients` / `/ingredients/{id}` | 查询原料 |
| PUT | `/ingredients/{id}` | 修订原料，须带 `expected_version` |
| POST | `/recipes` | 创建配方草稿 `{name, components[]}` |
| GET | `/recipes` / `/recipes/{id}` | 查询配方 |
| PUT | `/recipes/{id}` | 修订配方，须带 `expected_version` |
| POST | `/recipes/{id}/publish` | 发布生成不可变标签（可带 `expected_version` 锚定所见版本） |
| GET | `/recipes/{id}/labels` | 某配方的全部标签 |
| GET | `/labels/{id}` | 查询标签 |

组分格式：`{"type": "ingredient"|"recipe", "id": <正整数>, "grams": <正数>}`。

错误统一为 `{"error": {"code", "message", "details?"}}`，主要 code：
`VERSION_REQUIRED`(400)、`VERSION_CONFLICT`(409)、`NON_POSITIVE_AMOUNT`(422)、
`UNKNOWN_REFERENCE`(422)、`CYCLE_DETECTED`(422)、`ZERO_TOTAL`(422)、
`CONCURRENT_MODIFICATION`(409)、`NOT_FOUND`(404)。

## HTTP 调用示例

```bash
BASE=http://localhost:8000

# 1. 创建原料
curl -s -X POST $BASE/ingredients -d '{"name":"面粉","allergens":["gluten"]}'
# => {"id":1,"name":"面粉","allergens":["gluten"],"version":1,...}

# 2. 修订必须带所见版本；版本不符 -> 409 且不改稿
curl -s -X PUT $BASE/ingredients/1 \
  -d '{"name":"面粉","allergens":["gluten"],"expected_version":1}'

# 3. 创建配方：按克引用原料与子配方
curl -s -X POST $BASE/recipes -d '{"name":"海绵蛋糕","components":[
  {"type":"ingredient","id":1,"grams":50},
  {"type":"ingredient","id":2,"grams":50}]}'
curl -s -X POST $BASE/recipes -d '{"name":"蛋糕","components":[
  {"type":"ingredient","id":1,"grams":100},
  {"type":"recipe","id":1,"grams":200}]}'

# 4. 发布：递归检查 + 折算 + 汇总，生成不可变标签
curl -s -X POST $BASE/recipes/2/publish -d '{"expected_version":1}'
```

发布返回的标签（节选）：

```json
{
  "id": 1, "recipe_id": 2, "recipe_version": 1, "total_grams": 300,
  "allergens": {
    "gluten": {"total_grams": 200, "ingredients": [
      {"ingredient_id": 1, "name": "面粉", "grams": 200,
       "paths": ["蛋糕", "蛋糕 > 海绵蛋糕"]}]}
  },
  "dependency_versions": {
    "recipes": [{"id": 1, "name": "海绵蛋糕", "version": 1}, ...],
    "ingredients": [{"id": 1, "name": "面粉", "version": 1}, ...]
  }
}
```

上例中：海绵蛋糕总重 100g，被引用 200g，折算系数为 2；面粉一条路径直接出现（100g）、
一条经海绵蛋糕折算（50g×2=100g），同一原料多路径合计为 200g。

## 目录结构

```
app/
  db.py        # SQLite 连接与 schema（含标签不可变触发器）
  service.py   # 业务逻辑：乐观锁修订、一致视图发布、递归汇总
  server.py    # HTTP 层（标准库 http.server）
tests/test_service.py   # 业务规则测试
examples/demo.sh        # HTTP 调用示例脚本
Dockerfile / entrypoint.sh / docker-compose.yml
```
