# Telegram Bot Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bot.py .

# Environment variables (can be overridden)
ENV TELEGRAM_BOT_TOKEN=""
ENV SILLYTAVERN_URL="http://sillytavern:8000"
ENV ALLOWED_USER_ID="0"
ENV WEBHOOK_URL=""
ENV LOG_LEVEL="INFO"
ENV PORT="8443"

# Expose webhook port
EXPOSE 8443

# Run the bot
CMD ["python", "bot.py"]
