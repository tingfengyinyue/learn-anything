FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# 国内网络默认走镜像源；海外可以 docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# 数据库放在挂载卷里，容器重建不丢数据
ENV LA_DATABASE_URL=sqlite:////data/learn.db
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["learn", "server", "--host", "0.0.0.0", "--port", "8765"]
