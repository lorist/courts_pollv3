# syntax=docker/dockerfile:1

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy code and config
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port (optional, if you add a web interface)
EXPOSE 8000

# Set environment
ENV PYTHONUNBUFFERED=1

# Run script
CMD ["python", "vmr_cleanup.py"]
