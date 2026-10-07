FROM python:3.12-slim

WORKDIR /app

# 构建期预装依赖（本服务仅标准库，此步为扩展预留）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV DB_PATH=/data/app.db \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# 启动时安装依赖并初始化数据库，然后运行服务
ENTRYPOINT ["./entrypoint.sh"]
