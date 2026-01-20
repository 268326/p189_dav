FROM python:3.12-slim

WORKDIR /app

# 设置 Python 不缓冲输出
ENV PYTHONUNBUFFERED=1

# 设置时区
ENV TZ=Asia/Shanghai

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ .

# 创建数据目录
RUN mkdir -p /app/db

# 暴露端口
EXPOSE 8515

# 环境变量
ENV PORT=8515
ENV HOST=0.0.0.0
ENV ENV_189_COOKIES_FILE=/app/db/cookies.txt

# 启动命令
CMD ["python", "main.py"]
