FROM python:3.9-alpine
WORKDIR /app
# 用清华 PyPI 镜像加速（国内直连 pypi.org 慢）
COPY requirements.txt .
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 5000
CMD ["python", "app.py"]
