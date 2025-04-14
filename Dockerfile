FROM python:3.11-slim

WORKDIR /app

# Copy all code and certs
COPY . /app

RUN mkdir -p /app/cert
# Ensure permissions on certs if needed
RUN chmod -R 600 /app/cert

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1

CMD ["python", "vmr_cleaner.py"]
