FROM acrcentralusvodev.azurecr.io/base/python:3.12.3-slim


WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python","run.py"]
 
