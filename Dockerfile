FROM mcr.microsoft.com/playwright/python:v1.62.0-noble
WORKDIR /app
COPY app.py .
EXPOSE 8000
CMD ["python3", "app.py"]
