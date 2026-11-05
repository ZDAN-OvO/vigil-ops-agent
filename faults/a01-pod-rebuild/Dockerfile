FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY app.py /app/app.py
USER 1000:1000
EXPOSE 8080
CMD ["python", "-I", "/app/app.py"]
