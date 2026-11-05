FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY proxy.py /app/proxy.py
USER 1000:1000
EXPOSE 6379
CMD ["python", "-I", "/app/proxy.py"]
