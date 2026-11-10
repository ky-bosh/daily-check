FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.48.0 awslambdaric boto3
COPY monitor.py aws/lambda_handler.py /app/
ENV HOME=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CHROMIUM_ARGS="--no-sandbox --single-process --no-zygote --disable-gpu --disable-dev-shm-usage"
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["lambda_handler.handler"]
