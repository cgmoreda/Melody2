FROM python:3.13-slim

# Install system dependencies (ffmpeg is required for voice alert, git is required for installing git pip dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Start the bot
CMD ["python", "main.py"]
