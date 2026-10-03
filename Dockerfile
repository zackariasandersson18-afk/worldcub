# tradebot always-on server. State lives in the tradebot-state branch, so the
# container can be replaced at any time; /data only caches the checkout.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tradebot tradebot
ENV PYTHONUNBUFFERED=1
VOLUME /data
# GITHUB_TOKEN (contents: read & write on the repo) is required to push state.
# BINANCE_TESTNET_API_KEY/SECRET switch the account from paper to the testnet.
CMD ["python", "-m", "tradebot", "serve", "--state-dir", "/data/state", \
     "--repo", "zackariasandersson18-afk/worldcub", "--ignore-gates"]
