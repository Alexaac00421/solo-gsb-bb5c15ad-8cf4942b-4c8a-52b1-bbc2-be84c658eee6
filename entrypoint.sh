#!/bin/sh
set -e

# 启动时确保依赖就绪（requirements.txt 目前为空，仅标准库；此处作为统一安装入口）
pip install --no-cache-dir -r requirements.txt

# 启动服务；服务启动时会自动初始化数据库（建表 + WAL + 不可变标签触发器）
exec python -m app.server
