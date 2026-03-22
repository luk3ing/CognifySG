FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
ARG TOKEN
ARG ADMIN_CHAT_ID
ENV TOKEN=$TOKEN
ENV ADMIN_CHAT_ID=$ADMIN_CHAT_ID
CMD ["python", "main.py"]
