FROM python:3.12-slim

WORKDIR /app

# 安装依赖（构建期安装一次；Compose 启动命令中也会再执行一次以保证“启动时安装依赖”）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PORT=8080 \
    DB_PATH=/data/kitchen.db

EXPOSE 8080
VOLUME ["/data"]

# 启动时初始化数据库（幂等，见 app/main.py -> db.init_db）并启动服务
CMD ["python", "-m", "app.main"]
