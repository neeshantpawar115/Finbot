FROM python:3.12-slim
WORKDIR /app
COPY index.html app.py llm.py tools.py ./
COPY market ./market
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "app.py"]
