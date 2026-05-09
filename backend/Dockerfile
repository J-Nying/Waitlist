FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install fastapi uvicorn requests pymysql sqlalchemy twilio sendgrid python-dotenv

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]