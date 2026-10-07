# 中央厨房配方过敏原标签服务

Python + SQLite 实现的配方过敏原标签服务，提供 HTTP JSON API。

- **原料草稿**：记录名称与过敏原列表。
- **配方草稿**：按克数引用原料或其他配方（可多层嵌套）。
- **乐观锁修订**：每次修订必须携带客户端所见 `version`；版本冲突（409）、
  非正用量（422）、不存在的引用（422）一律拒绝，且不在草稿上留下任何修改。
- **发布**：在同一事务（同一一致视图）内递归展开依赖，检测循环引用与零总重，
  子配方用量按其自身总克数折算，按过敏原汇总每种原料的克数与来源路径；
  同一原料经多条路径出现时克数合计、路径全保留。发布期间依赖被改动则整体
  失败回滚，不留半成品。
- **标签不可变**：发布成功即生成不可变标签，并记录所用全部依赖的版本快照；
  后续任何修订都不会改动旧标签（数据库触发器强制）。

## 快速开始

### Docker Compose（推荐，一键启动）

```bash
docker compose up --build
```

启动命令会在容器启动时执行 `pip install -r requirements.txt` 安装依赖，
服务启动时自动初始化数据库（幂等）。数据持久化在命名卷 `kitchen-data`。

### 本地运行

```bash
pip install -r requirements.txt   # 仅标准库，无第三方依赖
python -m app.main                # 启动时自动初始化数据库
```

### 运行测试

```bash
python3 tests/smoke_test.py       # 端到端覆盖全部规则（34 项断言）
```

## 配置

| 环境变量  | 默认值               | 说明                 |
|-----------|----------------------|----------------------|
| `PORT`    | `8080`               | 服务监听端口         |
| `DB_PATH` | `/data/kitchen.db`   | SQLite 数据库文件路径 |

## API 地址

服务地址：**http://localhost:8080**（Compose 将宿主机 8080 映射到容器 8080）

| 方法 | 路径                      | 说明                                     |
|------|---------------------------|------------------------------------------|
| POST | `/ingredients`            | 新建原料草稿 `{name, allergens}`         |
| GET  | `/ingredients/{id}`       | 查询原料                                 |
| PUT  | `/ingredients/{id}`       | 修订原料 `{name, allergens, version}`    |
| POST | `/recipes`                | 新建配方草稿 `{name, components}`        |
| GET  | `/recipes/{id}`           | 查询配方                                 |
| PUT  | `/recipes/{id}`           | 修订配方 `{name, components, version}`   |
| POST | `/recipes/{id}/publish`   | 发布标签 `{version}`                     |
| GET  | `/recipes/{id}/labels`    | 列出该配方全部标签                       |
| GET  | `/labels/{id}`            | 查询标签（不可变）                       |
| GET  | `/healthz`                | 健康检查                                 |

`components` 元素为 `{"ingredient_id": N, "grams": X}` 或
`{"recipe_id": N, "grams": X}`（二选一），`grams` 必须为正数。

### 错误响应

统一格式 `{"error": {"code": ..., "message": ...}}`：

| HTTP | code               | 含义                           |
|------|--------------------|--------------------------------|
| 400  | `validation`       | 请求体/字段不合法              |
| 404  | `not_found`        | 资源不存在                     |
| 409  | `version_conflict` | 所见版本与当前版本冲突         |
| 409  | `stale_dependency` | 发布期间依赖发生变化           |
| 422  | `invalid_amount`   | 非正用量                       |
| 422  | `missing_reference`| 引用的原料/配方不存在          |
| 422  | `cycle_detected`   | 发布时发现循环引用             |
| 422  | `zero_total_weight`| 发布时配方总克数为 0           |

## 调用示例

完整可运行示例见 [examples/curl_examples.sh](examples/curl_examples.sh)
（`BASE=http://localhost:8080 bash examples/curl_examples.sh`）。节选：

```bash
# 1. 建原料
curl -X POST localhost:8080/ingredients -H 'Content-Type: application/json' \
  -d '{"name": "面粉", "allergens": ["gluten"]}'        # => {"id": 1, "version": 1, ...}

# 2. 建配方（引用原料 1 与子配方，按克）
curl -X POST localhost:8080/recipes -H 'Content-Type: application/json' -d '{
  "name": "蛋糕",
  "components": [{"recipe_id": 1, "grams": 600}, {"ingredient_id": 1, "grams": 50}]
}'

# 3. 修订（必须带所见版本；过期版本 => 409 且不改稿）
curl -X PUT localhost:8080/ingredients/1 -H 'Content-Type: application/json' \
  -d '{"name": "高筋面粉", "allergens": ["gluten"], "version": 1}'

# 4. 发布（带配方所见版本）
curl -X POST localhost:8080/recipes/2/publish -H 'Content-Type: application/json' \
  -d '{"version": 1}'
```

发布返回的标签示例（子配方按自身总克数折算；同一原料多路径合计）：

```json
{
  "label_id": 1,
  "recipe_id": 2,
  "recipe_version": 1,
  "total_grams": 650.0,
  "allergens": {
    "gluten": {
      "total_grams": 425.0,
      "ingredients": [
        {"ingredient_id": 1, "name": "高筋面粉", "grams": 425.0, "paths": [
          {"path": ["蛋糕(recipe:2)", "海绵胚(recipe:1)", "高筋面粉(ingredient:1)"], "grams": 375.0},
          {"path": ["蛋糕(recipe:2)", "高筋面粉(ingredient:1)"], "grams": 50.0}
        ]}
      ]
    }
  },
  "dependencies": {
    "recipes": {"1": 1, "2": 1},
    "ingredients": {"1": 2}
  },
  "published_at": "2026-10-07T13:48:20.007Z"
}
```

## 实现要点

- **一致性**：所有写操作（修订、发布）都在 `BEGIN IMMEDIATE` 事务中执行，
  发布期间持有 SQLite 写锁，读到的依赖是同一一致视图；提交前再显式复核
  所有依赖版本，任一变化即回滚（`stale_dependency`），不留半成品。
- **折算**：子配方以 `用量 / 子配方自身总克数` 为系数递归折算到原料。
- **不可变标签**：`labels` 表由触发器禁止 UPDATE/DELETE；标签内嵌发布时的
  汇总内容与全部依赖版本快照，后续修订不影响旧标签。
- **零依赖**：仅用 Python 标准库（`http.server` / `sqlite3`），
  `requirements.txt` 为空占位，启动命令仍会执行安装步骤。

## 目录结构

```
app/
  main.py       HTTP 层（路由、JSON、错误映射）
  service.py    业务逻辑（乐观锁修订、递归发布、汇总）
  db.py         SQLite 连接与 schema（含不可变触发器）
tests/smoke_test.py   端到端测试（34 项断言）
examples/curl_examples.sh  HTTP 调用示例
Dockerfile / docker-compose.yml
```
